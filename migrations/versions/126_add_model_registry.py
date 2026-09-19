"""Add model_registry_mst table

Revision ID: 126_add_model_registry
Revises: 125_create_sales_inquiries_table
Create Date: 2026-04-10

Creates the model_registry_mst table for tracking EFS-cached HuggingFace models.
A partial unique index on (model_id, revision, COALESCE(tenant_code, '')) prevents
duplicate download records for the same model/revision/tenant combination.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "126_add_model_registry"
down_revision: Union[str, None] = "125_create_sales_inquiries_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_registry_mst",
        # BaseModel columns
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Column("is_deleted", sa.Boolean(), default=False),
        sa.Column("is_active", sa.Boolean(), default=True),

        # Model identity
        sa.Column("model_id", sa.String(512), nullable=False,
                  comment="HuggingFace model ID (e.g., meta-llama/Llama-3.1-8B-Instruct)"),
        sa.Column("revision", sa.String(40), nullable=False,
                  comment="Resolved HuggingFace commit SHA or 'main'"),
        sa.Column("efs_path", sa.String(512), nullable=False,
                  comment="PVC-root-relative path (no leading slash)"),

        # Status
        sa.Column("download_status", sa.String(20), nullable=False, server_default="pending",
                  comment="pending | downloading | ready | failed"),
        sa.Column("error_message", sa.Text(), nullable=True,
                  comment="Error detail when download_status=failed"),

        # Ownership — NULL = shared across all tenants
        sa.Column("tenant_code", sa.String(100), nullable=True,
                  comment="Tenant code for private models; NULL for shared public models"),

        # Optional metadata
        sa.Column("size_gb", sa.Float(), nullable=True,
                  comment="Approximate model size in GB"),

        sa.PrimaryKeyConstraint("id"),
        comment="Tracks EFS-cached HuggingFace models per tenant (or shared)",
    )

    op.create_index("ix_model_registry_code", "model_registry_mst", ["code"], unique=True)
    op.create_index("ix_model_registry_model_id", "model_registry_mst", ["model_id"])
    op.create_index("ix_model_registry_status", "model_registry_mst", ["download_status"])

    # Unique index using COALESCE to handle NULL tenant_code correctly.
    # Standard UniqueConstraint treats two NULLs as distinct; COALESCE normalises them.
    op.execute("""
        CREATE UNIQUE INDEX uq_model_registry_model_revision_tenant
        ON model_registry_mst (model_id, revision, COALESCE(tenant_code, ''))
        WHERE is_deleted = FALSE
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_model_registry_model_revision_tenant")
    op.drop_index("ix_model_registry_status", table_name="model_registry_mst")
    op.drop_index("ix_model_registry_model_id", table_name="model_registry_mst")
    op.drop_index("ix_model_registry_code", table_name="model_registry_mst")
    op.drop_table("model_registry_mst")
