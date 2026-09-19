"""Make infrastructure_mst.applications_mst_code nullable for tenant-level shared clusters

Revision ID: 135_make_infrastructure_mst_applications_code_nullable
Revises: 134_add_workspace_tables
Create Date: 2026-05-12

Changes:
  - ALTER COLUMN applications_mst_code on infrastructure_mst to allow NULL
    (required for tenant-level shared EKS clusters that are not scoped to a single application)
"""

from alembic import op
import sqlalchemy as sa

revision = "135_make_infrastructure_mst_applications_code_nullable"
down_revision = "134_add_workspace_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "infrastructure_mst",
        "applications_mst_code",
        existing_type=sa.VARCHAR(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "infrastructure_mst",
        "applications_mst_code",
        existing_type=sa.VARCHAR(),
        nullable=False,
    )
