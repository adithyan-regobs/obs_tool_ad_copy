"""make resource_group_mst_code optional in infrastructure_mst

Revision ID: 029_make_rg_optional
Revises: 028_infra_gitops_tracking
Create Date: 2025-01-18

Changes resource_group_mst_code column to allow NULL values in infrastructure_mst table.
This allows infrastructure resources to be created without an associated resource group.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '029_make_rg_optional'
down_revision = '028_infra_gitops_tracking'
branch_labels = None
depends_on = None


def upgrade():
    # Alter resource_group_mst_code column to allow NULL
    op.alter_column('infrastructure_mst', 'resource_group_mst_code',
                    existing_type=sa.String(100),
                    nullable=True)


def downgrade():
    # Note: This downgrade may fail if there are NULL values in the column
    # You would need to update those records first before running downgrade
    op.alter_column('infrastructure_mst', 'resource_group_mst_code',
                    existing_type=sa.String(100),
                    nullable=False)
