"""Add alb_selection column to sidecar_configs

This migration:
1. Adds alb_selection column to sidecar_configs table
2. Drops old unique constraint
3. Creates new unique constraint including alb_selection

Revision ID: 050
Revises: 049
Create Date: 2024-11-28
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '050_add_alb_selection_to_sidecar_configs'
down_revision = '049_add_alb_selection_column'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Add alb_selection column with default value
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

    # Step 2: Drop old unique constraint
    op.drop_constraint(
        'uq_sidecar_config_app_rg_env_region_name',
        'sidecar_configs',
        type_='unique'
    )

    # Step 3: Create new unique constraint including alb_selection
    op.create_unique_constraint(
        'uq_sidecar_config_app_rg_env_region_alb_name',
        'sidecar_configs',
        ['applications_mst_code', 'resource_group_mst_code', 'environment', 'region_mst_code', 'alb_selection', 'name']
    )

    # Remove server default after migration
    op.alter_column('sidecar_configs', 'alb_selection', server_default=None)


def downgrade() -> None:
    # Drop new constraint
    op.drop_constraint(
        'uq_sidecar_config_app_rg_env_region_alb_name',
        'sidecar_configs',
        type_='unique'
    )

    # Recreate old constraint
    op.create_unique_constraint(
        'uq_sidecar_config_app_rg_env_region_name',
        'sidecar_configs',
        ['applications_mst_code', 'resource_group_mst_code', 'environment', 'region_mst_code', 'name']
    )

    # Drop alb_selection column
    op.drop_column('sidecar_configs', 'alb_selection')
