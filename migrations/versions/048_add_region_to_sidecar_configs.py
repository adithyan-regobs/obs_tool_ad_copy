"""add region_mst_code to sidecar_configs

Revision ID: 048_add_region_to_sidecar_configs
Revises: 047_update_db_user_management_prompt_paths
Create Date: 2025-11-28

Adds region_mst_code column to sidecar_configs table for regional scoping.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '048_add_region_to_sidecar_configs'
down_revision = '047_update_db_user_management_prompt_paths'
branch_labels = None
depends_on = None


def upgrade():
    # Add region_mst_code column to sidecar_configs
    op.add_column(
        'sidecar_configs',
        sa.Column('region_mst_code', sa.String(100), nullable=True,
                  comment="Region this sidecar belongs to")
    )

    # Update existing rows with a default region (mumbai is commonly used)
    op.execute("UPDATE sidecar_configs SET region_mst_code = 'mumbai' WHERE region_mst_code IS NULL")

    # Make the column NOT NULL after populating
    op.alter_column('sidecar_configs', 'region_mst_code', nullable=False)

    # Create foreign key constraint
    op.create_foreign_key(
        'fk_sidecar_config_region',
        'sidecar_configs', 'region_mst',
        ['region_mst_code'], ['code'],
        ondelete='CASCADE'
    )

    # Create index for region
    op.create_index('idx_sidecar_config_region', 'sidecar_configs', ['region_mst_code'])

    # Drop old unique constraint and create new one with region
    op.drop_constraint('uq_sidecar_config_app_rg_env_name', 'sidecar_configs', type_='unique')
    op.create_unique_constraint(
        'uq_sidecar_config_app_rg_env_region_name',
        'sidecar_configs',
        ['applications_mst_code', 'resource_group_mst_code', 'environment', 'region_mst_code', 'name']
    )


def downgrade():
    # Drop new unique constraint and recreate old one
    op.drop_constraint('uq_sidecar_config_app_rg_env_region_name', 'sidecar_configs', type_='unique')
    op.create_unique_constraint(
        'uq_sidecar_config_app_rg_env_name',
        'sidecar_configs',
        ['applications_mst_code', 'resource_group_mst_code', 'environment', 'name']
    )

    # Drop index and foreign key
    op.drop_index('idx_sidecar_config_region', 'sidecar_configs')
    op.drop_constraint('fk_sidecar_config_region', 'sidecar_configs', type_='foreignkey')

    # Drop the column
    op.drop_column('sidecar_configs', 'region_mst_code')
