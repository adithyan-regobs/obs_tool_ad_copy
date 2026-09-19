"""Add tenant_mst_code to service_configs table

Revision ID: 045_add_tenant_to_service_configs
Revises: 044_remove_app_info_column
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '045_add_tenant_to_service_configs'
down_revision: Union[str, None] = '044_remove_app_info_column'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add tenant_mst_code column to service_configs for multi-tenant isolation."""
    # Add tenant_mst_code column (nullable initially for existing data)
    op.add_column('service_configs', sa.Column(
        'tenant_mst_code',
        sa.String(100),
        nullable=True,
        comment="Tenant this configuration belongs to (from JWT)"
    ))

    # Add FK constraint
    op.create_foreign_key(
        'fk_service_configs_tenant_mst',
        'service_configs', 'tenants_mst',
        ['tenant_mst_code'], ['code'],
        ondelete='CASCADE'
    )

    # Create index for tenant filtering
    op.create_index(
        'idx_service_configs_tenant',
        'service_configs',
        ['tenant_mst_code']
    )

    # Drop old unique constraint
    op.drop_constraint('uq_service_config_service_env_region', 'service_configs', type_='unique')

    # Create new unique constraint including tenant
    op.create_unique_constraint(
        'uq_service_config_tenant_service_env_region',
        'service_configs',
        ['tenant_mst_code', 'services_mst_code', 'environment', 'region_mst_code']
    )


def downgrade() -> None:
    """Remove tenant_mst_code column from service_configs."""
    # Drop new unique constraint
    op.drop_constraint('uq_service_config_tenant_service_env_region', 'service_configs', type_='unique')

    # Recreate old unique constraint
    op.create_unique_constraint(
        'uq_service_config_service_env_region',
        'service_configs',
        ['services_mst_code', 'environment', 'region_mst_code']
    )

    # Drop index
    op.drop_index('idx_service_configs_tenant', table_name='service_configs')

    # Drop FK constraint
    op.drop_constraint('fk_service_configs_tenant_mst', 'service_configs', type_='foreignkey')

    # Drop column
    op.drop_column('service_configs', 'tenant_mst_code')
