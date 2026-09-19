"""Add region_mst_code to service_configs table

Revision ID: 043_add_region_to_service_configs
Revises: 042_add_region_mst_table
Create Date: 2025-01-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '043_add_region_to_service_configs'
down_revision: Union[str, None] = '042_add_region_mst_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add region_mst_code column (nullable initially for existing data)
    op.add_column('service_configs', sa.Column(
        'region_mst_code', sa.String(100), nullable=True
    ))

    # Add FK constraint
    op.create_foreign_key(
        'fk_service_configs_region_mst',
        'service_configs', 'region_mst',
        ['region_mst_code'], ['code'],
        ondelete='CASCADE'
    )

    # Drop old unique constraint
    op.drop_constraint('uq_service_config_service_env', 'service_configs', type_='unique')

    # Create new unique constraint (with region)
    op.create_unique_constraint(
        'uq_service_config_service_env_region',
        'service_configs',
        ['services_mst_code', 'environment', 'region_mst_code']
    )


def downgrade() -> None:
    # Drop new unique constraint
    op.drop_constraint('uq_service_config_service_env_region', 'service_configs', type_='unique')

    # Drop FK constraint
    op.drop_constraint('fk_service_configs_region_mst', 'service_configs', type_='foreignkey')

    # Drop column
    op.drop_column('service_configs', 'region_mst_code')

    # Recreate old unique constraint
    op.create_unique_constraint(
        'uq_service_config_service_env',
        'service_configs',
        ['services_mst_code', 'environment']
    )
