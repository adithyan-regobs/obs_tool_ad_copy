"""Add region_mst_code to infrastructure_mst table

Revision ID: 046_add_region_to_infrastructure_mst
Revises: 045_add_tenant_to_service_configs
Create Date: 2024-11-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '046_add_region_to_infrastructure_mst'
down_revision: Union[str, None] = '045_add_tenant_to_service_configs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Insert US region for each tenant that doesn't have one
    op.execute("""
        INSERT INTO region_mst (code, name, description, tenants_mst_code, is_deleted, is_active, created_at)
        SELECT
            'region-' || t.code || '-us',
            'US',
            'United States region',
            t.code,
            false,
            true,
            NOW()
        FROM tenants_mst t
        WHERE NOT EXISTS (
            SELECT 1 FROM region_mst r
            WHERE r.tenants_mst_code = t.code AND r.is_deleted = false
        )
        AND t.is_deleted = false
    """)

    # Step 2: Add region_mst_code column as nullable first
    op.add_column(
        'infrastructure_mst',
        sa.Column(
            'region_mst_code',
            sa.String(100),
            nullable=True,
            comment='Business region this infrastructure belongs to'
        )
    )

    # Step 3: Update existing infrastructure_mst rows with matching region from their tenant
    op.execute("""
        UPDATE infrastructure_mst im
        SET region_mst_code = (
            SELECT r.code FROM region_mst r
            WHERE r.tenants_mst_code = im.tenants_mst_code
            AND r.is_deleted = false
            ORDER BY r.created_at ASC
            LIMIT 1
        )
        WHERE im.region_mst_code IS NULL
    """)

    # Step 4: Add foreign key constraint
    op.create_foreign_key(
        'fk_infrastructure_mst_region_mst_code',
        'infrastructure_mst',
        'region_mst',
        ['region_mst_code'],
        ['code'],
        ondelete='CASCADE'
    )

    # Step 5: Create index for faster lookups
    op.create_index(
        'ix_infrastructure_mst_region_mst_code',
        'infrastructure_mst',
        ['region_mst_code']
    )

    # Step 6: Make column NOT NULL
    op.alter_column(
        'infrastructure_mst',
        'region_mst_code',
        nullable=False
    )


def downgrade() -> None:
    # Make column nullable first
    op.alter_column(
        'infrastructure_mst',
        'region_mst_code',
        nullable=True
    )

    # Drop index
    op.drop_index('ix_infrastructure_mst_region_mst_code', table_name='infrastructure_mst')

    # Drop foreign key
    op.drop_constraint('fk_infrastructure_mst_region_mst_code', 'infrastructure_mst', type_='foreignkey')

    # Drop column
    op.drop_column('infrastructure_mst', 'region_mst_code')
