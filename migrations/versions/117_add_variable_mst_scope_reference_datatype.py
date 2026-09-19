"""Add scope_type, referenced_variable_id, data_type, secret_provider, rename secret_ref to variable_mst

Revision ID: 117_add_variable_mst_scope_reference_datatype
Revises: 116_add_variable_mst
Create Date: 2026-03-13

Adds:
  - scope_type              : GLOBAL or INFRA enum
  - referenced_variable_id  : self-referencing FK for variable-to-variable references
  - data_type               : string, integer, boolean, json enum
  - secret_provider         : aws_secrets_manager, hashicorp_vault, aws_ssm, etc.
  - Rename secret_ref       → secret_cloud_identifier
  - CHECK constraint        : chk_scope_infra (GLOBAL → table_name/transaction_code NULL)
  - CHECK constraint        : chk_value_or_reference (value XOR referenced_variable_id)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "117_add_variable_mst_scope_reference_datatype"
down_revision: Union[str, None] = "116_add_variable_mst"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Create new enums ──────────────────────────────────────────────────
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE variable_scope_type_enum AS ENUM ('GLOBAL', 'INFRA');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
    """)

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE variable_data_type_enum AS ENUM ('string', 'integer', 'boolean', 'json');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
    """)

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE secret_provider_enum AS ENUM (
                'aws_secrets_manager', 'aws_ssm', 'hashicorp_vault',
                'azure_key_vault', 'gcp_secret_manager', 'k8s_secret'
            );
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # ── 2. Rename secret_ref → secret_cloud_identifier ────────────────────────
    op.alter_column(
        "variable_mst",
        "secret_ref",
        new_column_name="secret_cloud_identifier",
        comment="Provider-specific path/identifier (ARN, Vault path, SSM parameter name, K8s secret key)",
    )

    # ── 4. Add new columns ───────────────────────────────────────────────────
    op.add_column(
        "variable_mst",
        sa.Column(
            "secret_provider",
            sa.Enum(
                "aws_secrets_manager", "aws_ssm", "hashicorp_vault",
                "azure_key_vault", "gcp_secret_manager", "k8s_secret",
                name="secret_provider_enum", create_type=False,
            ),
            nullable=True,
            comment="External secret backend: aws_secrets_manager, hashicorp_vault, aws_ssm, etc.",
        ),
    )

    op.add_column(
        "variable_mst",
        sa.Column(
            "scope_type",
            sa.Enum("GLOBAL", "INFRA", name="variable_scope_type_enum", create_type=False),
            nullable=True,
            comment="GLOBAL (shared across infra) or INFRA (belongs to a specific resource)",
        ),
    )

    op.add_column(
        "variable_mst",
        sa.Column(
            "referenced_variable_id",
            sa.BigInteger(),
            nullable=True,
            comment="Points to another variable_mst row. Mutually exclusive with value",
        ),
    )

    op.add_column(
        "variable_mst",
        sa.Column(
            "data_type",
            sa.Enum("string", "integer", "boolean", "json", name="variable_data_type_enum", create_type=False),
            server_default="string",
            nullable=True,
            comment="Data type of the value: string, integer, boolean, json",
        ),
    )

    # ── 5. Backfill scope_type for existing rows ─────────────────────────────
    op.execute("""
        UPDATE variable_mst
        SET scope_type = CASE
            WHEN table_name IS NOT NULL AND transaction_code IS NOT NULL
                THEN 'INFRA'::variable_scope_type_enum
            ELSE 'GLOBAL'::variable_scope_type_enum
        END
        WHERE scope_type IS NULL;
    """)

    # ── 6. Make scope_type NOT NULL after backfill ───────────────────────────
    op.alter_column("variable_mst", "scope_type", nullable=False)

    # ── 7. Make data_type NOT NULL after default is set ──────────────────────
    op.execute("UPDATE variable_mst SET data_type = 'string' WHERE data_type IS NULL;")
    op.alter_column("variable_mst", "data_type", nullable=False)

    # ── 8. Add self-referencing foreign key ──────────────────────────────────
    op.create_foreign_key(
        "fk_variable_mst_referenced_variable",
        "variable_mst",
        "variable_mst",
        ["referenced_variable_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── 9. Add CHECK constraints ─────────────────────────────────────────────
    op.execute("""
        ALTER TABLE variable_mst
        ADD CONSTRAINT chk_scope_infra CHECK (
            (scope_type = 'GLOBAL' AND table_name IS NULL AND transaction_code IS NULL)
            OR
            (scope_type = 'INFRA' AND table_name IS NOT NULL AND transaction_code IS NOT NULL)
        );
    """)

    op.execute("""
        ALTER TABLE variable_mst
        ADD CONSTRAINT chk_value_or_reference CHECK (
            NOT (value IS NOT NULL AND referenced_variable_id IS NOT NULL)
        );
    """)

    # ── 10. Index on referenced_variable_id for reverse lookups ──────────────
    op.create_index(
        "ix_variable_mst_referenced",
        "variable_mst",
        ["referenced_variable_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_variable_mst_referenced", table_name="variable_mst")
    op.execute("ALTER TABLE variable_mst DROP CONSTRAINT IF EXISTS chk_value_or_reference;")
    op.execute("ALTER TABLE variable_mst DROP CONSTRAINT IF EXISTS chk_scope_infra;")
    op.drop_constraint("fk_variable_mst_referenced_variable", "variable_mst", type_="foreignkey")
    op.drop_column("variable_mst", "secret_provider")
    op.drop_column("variable_mst", "data_type")
    op.drop_column("variable_mst", "referenced_variable_id")
    op.drop_column("variable_mst", "scope_type")

    # Rename back: secret_cloud_identifier → secret_ref
    op.alter_column(
        "variable_mst",
        "secret_cloud_identifier",
        new_column_name="secret_ref",
    )

    sa.Enum("GLOBAL", "INFRA", name="variable_scope_type_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum("string", "integer", "boolean", "json", name="variable_data_type_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(
        "aws_secrets_manager", "aws_ssm", "hashicorp_vault",
        "azure_key_vault", "gcp_secret_manager", "k8s_secret",
        name="secret_provider_enum",
    ).drop(op.get_bind(), checkfirst=True)
