"""Add infrastructure_mst_code to service_configs

Revision ID: 069_add_infrastructure_mst_code_to_service_configs
Revises: 068_add_infra_vendor_enum_to_service_configs
Create Date: 2025-12-11

Adds infrastructure_mst_code column to service_configs table.
This allows linking a service configuration to a specific infrastructure instance (cluster).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '069_add_infrastructure_mst_code_to_service_configs'
down_revision: Union[str, None] = '068_add_infra_vendor_enum_to_service_configs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add infrastructure_mst_code column to service_configs
    op.add_column(
        'service_configs',
        sa.Column(
            'infrastructure_mst_code',
            sa.String(100),
            nullable=True,  # Nullable - not all configs will have an infrastructure assigned
            comment='Infrastructure instance (cluster) this config is deployed to'
        )
    )

    # Add foreign key constraint
    op.create_foreign_key(
        'fk_service_configs_infrastructure_mst_code',
        'service_configs',
        'infrastructure_mst',
        ['infrastructure_mst_code'],
        ['code'],
        ondelete='SET NULL'
    )

    # Create index for better query performance
    op.create_index(
        'idx_service_configs_infrastructure_mst_code',
        'service_configs',
        ['infrastructure_mst_code']
    )


def downgrade() -> None:
    # Drop index
    op.drop_index('idx_service_configs_infrastructure_mst_code', table_name='service_configs')

    # Drop foreign key
    op.drop_constraint('fk_service_configs_infrastructure_mst_code', 'service_configs', type_='foreignkey')

    # Drop column
    op.drop_column('service_configs', 'infrastructure_mst_code')
