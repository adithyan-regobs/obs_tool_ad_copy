"""Add resource_connection_mst table for canvas edges

Revision ID: 148_add_resource_connection_mst
Revises: 147_rename_secret_cloud_identifier_to_variable_cloud_identifier
Create Date: 2026-07-05

Changes:
  - CREATE TABLE resource_connection_mst — polymorphic edges between
    resources (service→infra, service→service, infra→infra) with optional
    permission and network_policy JSONB payloads.
    Edges were previously derived from variable_mst.referenced_variable_id;
    with variables no longer seeding infra attributes, edges get their own
    table.

Reuses existing enum types (workflow_source_table_enum, environment_enum).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "148_add_resource_connection_mst"
down_revision: Union[str, None] = "147_rename_secret_cloud_identifier_to_variable_cloud_identifier"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_workflow_source_table_enum = postgresql.ENUM(
    name="workflow_source_table_enum", create_type=False
)
_environment_enum = postgresql.ENUM(name="environment_enum", create_type=False)


def upgrade() -> None:
    op.create_table(
        "resource_connection_mst",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column(
            "source_transaction_code",
            sa.String(100),
            nullable=False,
            comment="Code of the consuming/initiating resource (e.g. service_config code)",
        ),
        sa.Column(
            "source_table_name",
            _workflow_source_table_enum,
            nullable=False,
            comment="Table the source code lives in (SERVICE_CONFIG, INFRASTRUCTURE, ...)",
        ),
        sa.Column(
            "target_transaction_code",
            sa.String(100),
            nullable=False,
            comment="Code of the dependency/provider resource (e.g. infrastructure_mst code)",
        ),
        sa.Column(
            "target_table_name",
            _workflow_source_table_enum,
            nullable=False,
            comment="Table the target code lives in (SERVICE_CONFIG, INFRASTRUCTURE, ...)",
        ),
        sa.Column(
            "permission",
            postgresql.JSONB(),
            nullable=True,
            comment="Access grants, discriminated by type (s3 actions, db grants, ...)",
        ),
        sa.Column(
            "network_policy",
            postgresql.JSONB(),
            nullable=True,
            comment="Network reachability config: security groups, ports, CIDRs, etc.",
        ),
        sa.Column(
            "environments_enum",
            _environment_enum,
            nullable=False,
            comment="Environment of both endpoints (validated equal at write time)",
        ),
        sa.Column("tenants_mst_code", sa.String(100), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("is_deleted", sa.Boolean(), nullable=True, default=False),
        sa.Column("is_active", sa.Boolean(), nullable=True, default=True),
        sa.ForeignKeyConstraint(
            ["tenants_mst_code"], ["tenants_mst.code"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint(
            "source_transaction_code",
            "source_table_name",
            "target_transaction_code",
            "target_table_name",
            "environments_enum",
            name="uq_resource_connection_edge",
        ),
    )

    # Edge fetches are by tenant + environment (canvas load), and by either
    # endpoint code (node detail / cleanup flows).
    op.create_index(
        "ix_resource_connection_tenant_env",
        "resource_connection_mst",
        ["tenants_mst_code", "environments_enum"],
    )
    op.create_index(
        "ix_resource_connection_source",
        "resource_connection_mst",
        ["source_transaction_code"],
    )
    op.create_index(
        "ix_resource_connection_target",
        "resource_connection_mst",
        ["target_transaction_code"],
    )


def downgrade() -> None:
    op.drop_index("ix_resource_connection_target", table_name="resource_connection_mst")
    op.drop_index("ix_resource_connection_source", table_name="resource_connection_mst")
    op.drop_index("ix_resource_connection_tenant_env", table_name="resource_connection_mst")
    op.drop_table("resource_connection_mst")
    # Enum types are shared with other tables — do not drop them here.
