"""Replace infra_vendor_accounts_mst_code with infrastructure_mst_code in services_mst

Revision ID: 018_infra_mst_fk
Revises: 017_tenants_not_null
Create Date: 2025-01-05 04:00:00.000000

This migration replaces the direct foreign key to infra_vendor_accounts_mst with
a foreign key to infrastructure_mst. This allows services to reference a specific
infrastructure instance, and through that, find the vendor account information.

Hierarchy for infrastructure lookup:
Service -> Infrastructure MST -> Infra Vendor Account -> Vendor Enum
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '018_infra_mst_fk'
down_revision: Union[str, None] = '017_tenants_not_null'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Replace infra_vendor_accounts_mst_code with infrastructure_mst_code
    """
    # Step 1: Drop the old foreign key constraint
    op.drop_constraint(
        'fk_services_mst_infra_vendor_accounts',
        'services_mst',
        type_='foreignkey'
    )

    # Step 2: Drop the old column
    op.drop_column('services_mst', 'infra_vendor_accounts_mst_code')

    # Step 3: Add new infrastructure_mst_code column (nullable for now)
    op.add_column(
        'services_mst',
        sa.Column('infrastructure_mst_code', sa.String(length=100), nullable=True)
    )

    # Step 4: Create foreign key constraint to infrastructure_mst
    op.create_foreign_key(
        'fk_services_mst_infrastructure_mst',
        'services_mst',
        'infrastructure_mst',
        ['infrastructure_mst_code'],
        ['code'],
        ondelete='RESTRICT'
    )

    # Note: Column remains nullable because:
    # 1. Existing services may not have infrastructure assigned yet
    # 2. Infrastructure can be configured at tenant/app/resource group level
    # 3. Allows flexible hierarchical lookup


def downgrade() -> None:
    """
    Revert back to infra_vendor_accounts_mst_code
    """
    # Step 1: Drop the new foreign key constraint
    op.drop_constraint(
        'fk_services_mst_infrastructure_mst',
        'services_mst',
        type_='foreignkey'
    )

    # Step 2: Drop the new column
    op.drop_column('services_mst', 'infrastructure_mst_code')

    # Step 3: Re-add the old column
    op.add_column(
        'services_mst',
        sa.Column('infra_vendor_accounts_mst_code', sa.String(length=100), nullable=True)
    )

    # Step 4: Recreate the old foreign key constraint
    op.create_foreign_key(
        'fk_services_mst_infra_vendor_accounts',
        'services_mst',
        'infra_vendor_accounts_mst',
        ['infra_vendor_accounts_mst_code'],
        ['code'],
        ondelete='RESTRICT'
    )
