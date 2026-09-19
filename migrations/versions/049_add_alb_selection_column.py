"""Add alb_selection column to service_configs

This migration:
1. Adds alb_selection column to service_configs table
2. Populates it from config JSONB field for existing rows
3. Drops old unique constraint
4. Creates new unique constraint including alb_selection

Revision ID: 049
Revises: 048
Create Date: 2024-11-28
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '049_add_alb_selection_column'
down_revision = '048_add_region_to_sidecar_configs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Add alb_selection column with default value
    op.add_column(
        'service_configs',
        sa.Column(
            'alb_selection',
            sa.String(50),
            nullable=False,
            server_default='existing_alb',
            comment='ALB selection type: no_alb, existing_alb, create_new_alb'
        )
    )

    # Step 2: Populate alb_selection from config JSONB for existing rows
    op.execute("""
        UPDATE service_configs
        SET alb_selection = COALESCE(config->>'alb_selection', 'existing_alb')
        WHERE config IS NOT NULL
    """)

    # Step 3: Drop old unique constraint
    op.drop_constraint(
        'uq_service_config_tenant_service_env_region',
        'service_configs',
        type_='unique'
    )

    # Step 4: Create new unique constraint including alb_selection
    op.create_unique_constraint(
        'uq_service_config_tenant_service_env_region_alb',
        'service_configs',
        ['tenant_mst_code', 'services_mst_code', 'environment', 'region_mst_code', 'alb_selection']
    )

    # Remove server default after data migration
    op.alter_column('service_configs', 'alb_selection', server_default=None)


def downgrade() -> None:
    # Drop new constraint
    op.drop_constraint(
        'uq_service_config_tenant_service_env_region_alb',
        'service_configs',
        type_='unique'
    )

    # Recreate old constraint
    op.create_unique_constraint(
        'uq_service_config_tenant_service_env_region',
        'service_configs',
        ['tenant_mst_code', 'services_mst_code', 'environment', 'region_mst_code']
    )

    # Drop alb_selection column
    op.drop_column('service_configs', 'alb_selection')
