"""Drop infra_vendor_enum and infrastructuretype_ref_code from services_mst

Revision ID: 071_drop_infra_columns_from_services_mst
Revises: 070_add_infra_vendor_to_infrastructure_mst
Create Date: 2025-12-11

Drops infra_vendor_enum and infrastructuretype_ref_code columns from services_mst table.
These fields have been moved to service_configs table to allow different vendors/infra types
per environment/configuration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '071_drop_infra_columns_from_services_mst'
down_revision: Union[str, None] = '070_add_basemodel_fields_to_dockerfile_workflow'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use raw SQL with IF EXISTS for safer index/constraint dropping
    connection = op.get_bind()

    # Drop indexes if they exist
    connection.execute(sa.text('DROP INDEX IF EXISTS idx_services_mst_vendor'))
    connection.execute(sa.text('DROP INDEX IF EXISTS idx_services_mst_vendor_region'))
    connection.execute(sa.text('DROP INDEX IF EXISTS idx_services_mst_infrastructuretype_ref_code'))

    # Drop foreign key constraint if it exists
    connection.execute(sa.text('''
        ALTER TABLE services_mst
        DROP CONSTRAINT IF EXISTS services_mst_infrastructuretype_ref_code_fkey
    '''))

    # Drop the columns
    op.drop_column('services_mst', 'infra_vendor_enum')
    op.drop_column('services_mst', 'infrastructuretype_ref_code')


def downgrade() -> None:
    # Re-add infrastructuretype_ref_code column
    op.add_column(
        'services_mst',
        sa.Column(
            'infrastructuretype_ref_code',
            sa.String(100),
            nullable=True  # Nullable for downgrade since we can't restore original values
        )
    )

    # Re-add infra_vendor_enum column
    infra_vendor_enum = postgresql.ENUM(
        'on_prem', 'aws', 'gcp', 'azure',
        name='infra_vendor_enum',
        create_type=False
    )
    op.add_column(
        'services_mst',
        sa.Column(
            'infra_vendor_enum',
            infra_vendor_enum,
            nullable=True,
            server_default='aws'
        )
    )

    # Re-create foreign key constraint
    op.create_foreign_key(
        'services_mst_infrastructuretype_ref_code_fkey',
        'services_mst',
        'infrastructuretype_ref',
        ['infrastructuretype_ref_code'],
        ['code']
    )

    # Re-create indexes
    op.create_index('idx_services_mst_infrastructuretype_ref_code', 'services_mst', ['infrastructuretype_ref_code'])
    op.create_index('idx_services_mst_vendor', 'services_mst', ['infra_vendor_enum'])
