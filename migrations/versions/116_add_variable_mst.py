"""Add variable_mst table and variable_ids columns

Revision ID: 116_add_variable_mst
Revises: 115_add_db_object_and_permission_mst
Create Date: 2026-03-13

variable_mst : Stores variables and secrets for any resource (service config,
               infrastructure, etc.) via polymorphic table_name + transaction_code.
               variable_type discriminates plaintext variables from external secrets.

Also adds variable_ids (ARRAY of BigInteger) to:
  - service_configs
  - infrastructure_mst
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

# revision identifiers, used by Alembic.
revision: str = "116_add_variable_mst"
down_revision: Union[str, None] = "115_add_db_object_and_permission_mst"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Create variable_type_enum if it doesn't exist ──────────────────
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE variable_type_enum AS ENUM ('VARIABLE', 'SECRET');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # ── 2. Create variable_mst table via raw SQL to avoid SQLAlchemy
    #       _on_table_create firing CREATE TYPE for already-existing enums ──
    op.execute("""
        CREATE TABLE IF NOT EXISTS variable_mst (
            id                  BIGSERIAL PRIMARY KEY,
            code                VARCHAR(100) NOT NULL,
            name                VARCHAR(255) NOT NULL,
            description         VARCHAR(500),
            created_at          TIMESTAMPTZ DEFAULT now(),
            updated_at          TIMESTAMPTZ DEFAULT now(),
            is_deleted          BOOLEAN DEFAULT FALSE,
            is_active           BOOLEAN DEFAULT TRUE,
            table_name          workflow_source_table_enum,
            transaction_code    VARCHAR(100),
            key                 VARCHAR(255) NOT NULL,
            value               TEXT,
            variable_type       variable_type_enum NOT NULL,
            secret_ref          VARCHAR(500),
            environments_enum   enviornment_enum,
            tenants_mst_code    VARCHAR(100) NOT NULL
                                    REFERENCES tenants_mst(code) ON DELETE CASCADE,
            metadata            JSONB,
            CONSTRAINT uq_variable_mst_code UNIQUE (code)
        );
    """)

    # Indexes for common query patterns
    op.create_index(
        "ix_variable_mst_owner",
        "variable_mst",
        ["table_name", "transaction_code"],
    )
    op.create_index(
        "ix_variable_mst_tenant",
        "variable_mst",
        ["tenants_mst_code"],
    )
    op.create_index(
        "ix_variable_mst_key",
        "variable_mst",
        ["table_name", "transaction_code", "key"],
    )

    # ── 3. Add variable_ids column to service_configs ─────────────────────
    op.add_column(
        "service_configs",
        sa.Column(
            "variable_ids",
            ARRAY(sa.BigInteger()),
            nullable=True,
            comment="List of variable_mst.id values associated with this service config",
        ),
    )

    # ── 4. Add variable_ids column to infrastructure_mst ──────────────────
    op.add_column(
        "infrastructure_mst",
        sa.Column(
            "variable_ids",
            ARRAY(sa.BigInteger()),
            nullable=True,
            comment="List of variable_mst.id values associated with this infrastructure resource",
        ),
    )


def downgrade() -> None:
    op.drop_column("infrastructure_mst", "variable_ids")
    op.drop_column("service_configs", "variable_ids")

    op.drop_index("ix_variable_mst_key", table_name="variable_mst")
    op.drop_index("ix_variable_mst_tenant", table_name="variable_mst")
    op.drop_index("ix_variable_mst_owner", table_name="variable_mst")
    op.drop_table("variable_mst")

    sa.Enum("VARIABLE", "SECRET", name="variable_type_enum").drop(
        op.get_bind(), checkfirst=True
    )
