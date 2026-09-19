"""Remove geo_loc_mst_code and alb_selection from sidecar_configs

Revision ID: 054_remove_geo_loc_alb_from_sidecar_configs
Revises: 053_remove_folder_from_pipeline_mst
Create Date: 2025-11-30

Sidecars are scoped at application level, not per geo-location/ALB.
This migration:
1. Drops the unique constraint with geo_loc and alb_selection
2. Drops the index and foreign key for geo_loc_mst_code
3. Removes geo_loc_mst_code and alb_selection columns
4. Creates new unique constraint: (app, resource_group, environment, name)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '054_remove_geo_loc_alb_from_sidecar_configs'
down_revision: Union[str, None] = '053_remove_folder_from_pipeline_mst'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Drop existing unique constraint
    op.drop_constraint(
        'uq_sidecar_config_app_rg_env_geo_loc_alb_name',
        'sidecar_configs',
        type_='unique'
    )

    # Step 2: Drop index on geo_loc_mst_code
    op.drop_index('idx_sidecar_config_geo_loc', table_name='sidecar_configs')

    # Step 3: Drop foreign key constraint for geo_loc_mst_code
    op.drop_constraint(
        'fk_sidecar_config_geo_loc',
        'sidecar_configs',
        type_='foreignkey'
    )

    # Step 4: Drop the columns
    op.drop_column('sidecar_configs', 'geo_loc_mst_code')
    op.drop_column('sidecar_configs', 'alb_selection')

    # Step 5: Create new unique constraint without geo_loc and alb_selection
    op.create_unique_constraint(
        'uq_sidecar_config_app_rg_env_name',
        'sidecar_configs',
        ['applications_mst_code', 'resource_group_mst_code', 'environment', 'name']
    )


def downgrade() -> None:
    # Step 1: Drop new unique constraint
    op.drop_constraint(
        'uq_sidecar_config_app_rg_env_name',
        'sidecar_configs',
        type_='unique'
    )

    # Step 2: Add columns back
    op.add_column(
        'sidecar_configs',
        sa.Column(
            'alb_selection',
            sa.String(50),
            nullable=False,
            server_default='existing_alb',
            comment='ALB selection type: no_alb, existing_alb, create_new_alb'
        )
    )
    op.add_column(
        'sidecar_configs',
        sa.Column(
            'geo_loc_mst_code',
            sa.String(100),
            nullable=False,
            server_default='mumbai',
            comment='Geographic location this sidecar belongs to'
        )
    )

    # Step 3: Remove server defaults
    op.alter_column('sidecar_configs', 'alb_selection', server_default=None)
    op.alter_column('sidecar_configs', 'geo_loc_mst_code', server_default=None)

    # Step 4: Create foreign key constraint for geo_loc_mst_code
    op.create_foreign_key(
        'fk_sidecar_config_geo_loc',
        'sidecar_configs',
        'geo_loc_mst',
        ['geo_loc_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    # Step 5: Create index on geo_loc_mst_code
    op.create_index(
        'idx_sidecar_config_geo_loc',
        'sidecar_configs',
        ['geo_loc_mst_code']
    )

    # Step 6: Recreate original unique constraint
    op.create_unique_constraint(
        'uq_sidecar_config_app_rg_env_geo_loc_alb_name',
        'sidecar_configs',
        ['applications_mst_code', 'resource_group_mst_code', 'environment', 'geo_loc_mst_code', 'alb_selection', 'name']
    )
