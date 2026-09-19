"""Add workspace_mst and workspace_user_map tables, workspace_code on applications_mst

Revision ID: 134_add_workspace_tables
Revises: 133_add_deletion_statuses_to_resource_status_enum
Create Date: 2026-05-09

Changes:
  - CREATE TYPE workspace_status_enum ('active', 'archived')
  - CREATE TYPE workspace_role_enum ('owner', 'admin', 'edit', 'read_only')
  - CREATE TABLE workspace_mst
  - CREATE TABLE workspace_user_mapping  (UNIQUE workspace_code + user_mst_code)
  - ALTER TABLE applications_mst ADD COLUMN workspace_code (nullable FK)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "134_add_workspace_tables"
down_revision: Union[str, None] = "133_add_deletion_statuses_to_resource_status_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Enum types ────────────────────────────────────────────────────────────
    workspace_status_enum = postgresql.ENUM(
        "active", "archived",
        name="workspace_status_enum",
        create_type=False,
    )
    workspace_status_enum.create(op.get_bind(), checkfirst=True)

    workspace_role_enum = postgresql.ENUM(
        "owner", "admin", "edit", "read_only",
        name="workspace_role_enum",
        create_type=False,
    )
    workspace_role_enum.create(op.get_bind(), checkfirst=True)

    # ── workspace_mst table ───────────────────────────────────────────────────
    op.create_table(
        "workspace_mst",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("tenants_mst_code", sa.String(100), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "archived",
                name="workspace_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="active",
        ),
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
    )
    op.create_index("ix_workspace_mst_tenants_mst_code", "workspace_mst", ["tenants_mst_code"])

    # ── workspace_user_mapping table ─────────────────────────────────────────
    op.create_table(
        "workspace_user_mapping",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("workspace_code", sa.String(100), nullable=False),
        sa.Column("user_mst_code", sa.String(100), nullable=False),
        sa.Column("tenants_mst_code", sa.String(100), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(
                "owner", "admin", "edit", "read_only",
                name="workspace_role_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="read_only",
        ),
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
            ["workspace_code"], ["workspace_mst.code"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_mst_code"], ["user_mst.code"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenants_mst_code"], ["tenants_mst.code"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint("workspace_code", "user_mst_code", name="uq_workspace_user_mapping"),
    )
    op.create_index("ix_workspace_user_mapping_workspace_code", "workspace_user_mapping", ["workspace_code"])
    op.create_index("ix_workspace_user_mapping_user_mst_code", "workspace_user_mapping", ["user_mst_code"])
    op.create_index("ix_workspace_user_mapping_tenants_mst_code", "workspace_user_mapping", ["tenants_mst_code"])

    # ── applications_mst: add workspace_code (nullable) ───────────────────────
    op.add_column(
        "applications_mst",
        sa.Column("workspace_code", sa.String(100), nullable=True),
    )
    op.create_foreign_key(
        "fk_applications_mst_workspace_code",
        "applications_mst", "workspace_mst",
        ["workspace_code"], ["code"],
        ondelete="SET NULL",
    )
    op.create_index("ix_applications_mst_workspace_code", "applications_mst", ["workspace_code"])


def downgrade() -> None:
    # ── applications_mst ─────────────────────────────────────────────────────
    op.drop_index("ix_applications_mst_workspace_code", table_name="applications_mst")
    op.drop_constraint("fk_applications_mst_workspace_code", "applications_mst", type_="foreignkey")
    op.drop_column("applications_mst", "workspace_code")

    # ── workspace_user_mapping ────────────────────────────────────────────────
    op.drop_index("ix_workspace_user_mapping_tenants_mst_code", table_name="workspace_user_mapping")
    op.drop_index("ix_workspace_user_mapping_user_mst_code", table_name="workspace_user_mapping")
    op.drop_index("ix_workspace_user_mapping_workspace_code", table_name="workspace_user_mapping")
    op.drop_table("workspace_user_mapping")

    # ── workspace_mst ─────────────────────────────────────────────────────────
    op.drop_index("ix_workspace_mst_tenants_mst_code", table_name="workspace_mst")
    op.drop_table("workspace_mst")

    # ── enum types ────────────────────────────────────────────────────────────
    op.execute("DROP TYPE IF EXISTS workspace_role_enum")
    op.execute("DROP TYPE IF EXISTS workspace_status_enum")
