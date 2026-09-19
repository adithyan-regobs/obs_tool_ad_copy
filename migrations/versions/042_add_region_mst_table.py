"""add region_mst table

Revision ID: 042_add_region_mst_table
Revises: 041_fix_general_case_is_deleted
Create Date: 2025-11-27

Business/Deployment regions (not AWS regions).
Tenant-specific regions used in Deployment tab's Region dropdown.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '042_add_region_mst_table'
down_revision = '041_fix_general_case_is_deleted'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create region_mst table for business/deployment regions
    """
    op.create_table(
        'region_mst',
        # BaseModel columns (inherited)
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('is_active', sa.Boolean(), nullable=True, server_default='true'),

        # RegionMstModel specific columns
        sa.Column('tenants_mst_code', sa.String(length=100), nullable=False),

        # Primary key
        sa.PrimaryKeyConstraint('id'),

        # Unique constraints
        sa.UniqueConstraint('code', name='uq_region_mst_code'),

        # Foreign keys
        sa.ForeignKeyConstraint(
            ['tenants_mst_code'],
            ['tenants_mst.code'],
            name='fk_region_mst_tenants_mst_code',
            ondelete='CASCADE'
        ),
    )

    # Create indexes for better query performance
    op.create_index('ix_region_mst_tenants_mst_code', 'region_mst', ['tenants_mst_code'])


def downgrade() -> None:
    """
    Drop region_mst table
    """
    op.drop_index('ix_region_mst_tenants_mst_code', table_name='region_mst')
    op.drop_table('region_mst')
