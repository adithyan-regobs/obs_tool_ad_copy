"""Remove region_ref_code, region_custom, and region_display columns from services_mst table

Revision ID: 051_remove_region_from_services_mst
Revises: 050_add_alb_selection_to_sidecar_configs
Create Date: 2024-11-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = '051_remove_region_from_services_mst'
down_revision: Union[str, None] = '050_add_alb_selection_to_sidecar_configs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove region-related columns and constraints from services_mst table."""
    conn = op.get_bind()

    # Get all constraint names for services_mst table
    result = conn.execute(text("""
        SELECT conname, contype
        FROM pg_constraint
        WHERE conrelid = 'services_mst'::regclass
    """))
    constraints = {row[0]: row[1] for row in result}

    # Drop check constraints if they exist
    if 'chk_services_region_xor' in constraints:
        op.drop_constraint('chk_services_region_xor', 'services_mst', type_='check')
    if 'chk_services_region_custom_onprem' in constraints:
        op.drop_constraint('chk_services_region_custom_onprem', 'services_mst', type_='check')

    # Find and drop foreign key constraint for region_ref_code (may have different naming)
    for conname, contype in constraints.items():
        if contype == 'f' and 'region_ref_code' in conname:
            op.drop_constraint(conname, 'services_mst', type_='foreignkey')
            break

    # Check which columns exist before dropping
    result = conn.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'services_mst'
        AND column_name IN ('region_display', 'region_ref_code', 'region_custom')
    """))
    existing_columns = [row[0] for row in result]

    # Drop columns if they exist
    if 'region_display' in existing_columns:
        op.drop_column('services_mst', 'region_display')
    if 'region_ref_code' in existing_columns:
        op.drop_column('services_mst', 'region_ref_code')
    if 'region_custom' in existing_columns:
        op.drop_column('services_mst', 'region_custom')


def downgrade() -> None:
    """Re-add region-related columns and constraints to services_mst table."""
    # Re-add region columns
    op.add_column('services_mst', sa.Column(
        'region_ref_code',
        sa.String(100),
        nullable=True
    ))
    op.add_column('services_mst', sa.Column(
        'region_custom',
        sa.String(100),
        nullable=True
    ))

    # Re-add computed column for unified region display
    op.add_column('services_mst', sa.Column(
        'region_display',
        sa.String(100),
        sa.Computed("COALESCE(region_custom, region_ref_code)")
    ))

    # Re-add foreign key constraint
    op.create_foreign_key(
        'services_mst_region_ref_code_fkey',
        'services_mst',
        'region_ref',
        ['region_ref_code'],
        ['code'],
        ondelete='RESTRICT'
    )

    # Re-add check constraints
    op.create_check_constraint(
        'chk_services_region_xor',
        'services_mst',
        "(region_ref_code IS NOT NULL AND region_custom IS NULL) OR "
        "(region_ref_code IS NULL AND region_custom IS NOT NULL)"
    )
    op.create_check_constraint(
        'chk_services_region_custom_onprem',
        'services_mst',
        "region_custom IS NULL OR infra_vendor_enum = 'on_prem'"
    )
