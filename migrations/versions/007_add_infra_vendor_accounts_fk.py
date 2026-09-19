"""Add infra_vendor_accounts_mst_code to services_mst

Revision ID: 007_infra_vendor_accounts_fk
Revises: 006_pipeline_tables
Create Date: 2025-11-04 22:16:42.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '007_infra_vendor_accounts_fk'
down_revision: Union[str, None] = '006_pipeline_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add infra_vendor_accounts_mst_code column to services_mst"""

    # Add infra_vendor_accounts_mst_code column
    op.add_column(
        'services_mst',
        sa.Column('infra_vendor_accounts_mst_code', sa.String(length=100), nullable=True)
    )

    # Create foreign key constraint
    op.create_foreign_key(
        'fk_services_mst_infra_vendor_accounts',
        'services_mst',
        'infra_vendor_accounts_mst',
        ['infra_vendor_accounts_mst_code'],
        ['code'],
        ondelete='RESTRICT'
    )


def downgrade() -> None:
    """Remove infra_vendor_accounts_mst_code column from services_mst"""

    # Drop foreign key constraint
    op.drop_constraint('fk_services_mst_infra_vendor_accounts', 'services_mst', type_='foreignkey')

    # Drop column
    op.drop_column('services_mst', 'infra_vendor_accounts_mst_code')
