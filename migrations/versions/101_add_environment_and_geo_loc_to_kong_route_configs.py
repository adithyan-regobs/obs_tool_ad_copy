"""add environment and geo_loc to kong_route_configs

Revision ID: 101_add_environment_and_geo_loc_to_kong_route_configs
Revises: 100_add_name_description_to_transaction_queue
Create Date: 2026-01-09

Changes:
1. Add environments_enum column (ENUM: dev/staging/qa/prod, nullable) - matches infrastructure_mst.environments_enum
2. Add geo_loc_mst_code column (VARCHAR 100, nullable, FK to geo_loc_mst) - matches infrastructure_mst.geo_loc_mst_code
3. These fields are needed for Kong routes to match the structure of infrastructure_mst
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '101_add_environment_and_geo_loc_to_kong_route_configs'
down_revision: Union[str, None] = '100_add_name_description_to_transaction_queue'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add environments_enum column to kong_route_configs using existing environment_enum type
    op.add_column(
        'kong_route_configs',
        sa.Column(
            'environments_enum',
            sa.Enum('dev', 'staging', 'qa', 'prod', name='environment_enum'),
            nullable=True,
            comment='Environment (dev/staging/qa/prod) - matches infrastructure_mst.environments_enum'
        )
    )

    # Add geo_loc_mst_code column to kong_route_configs
    op.add_column(
        'kong_route_configs',
        sa.Column(
            'geo_loc_mst_code',
            sa.String(100),
            nullable=True,
            comment='Geographic location code - matches infrastructure_mst.geo_loc_mst_code'
        )
    )

    # Create foreign key constraint to geo_loc_mst table
    op.create_foreign_key(
        'kong_route_configs_geo_loc_mst_code_fkey',
        'kong_route_configs',
        'geo_loc_mst',
        ['geo_loc_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    # Create index on geo_loc_mst_code for queries
    op.create_index(
        'idx_kong_route_configs_geo_loc_mst_code',
        'kong_route_configs',
        ['geo_loc_mst_code']
    )


def downgrade() -> None:
    # Remove in reverse order
    op.drop_index('idx_kong_route_configs_geo_loc_mst_code', table_name='kong_route_configs')
    op.drop_constraint('kong_route_configs_geo_loc_mst_code_fkey', 'kong_route_configs', type_='foreignkey')
    op.drop_column('kong_route_configs', 'geo_loc_mst_code')
    op.drop_column('kong_route_configs', 'environments_enum')
