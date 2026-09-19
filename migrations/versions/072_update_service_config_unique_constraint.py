"""Update service_config unique constraint to include infrastructure fields

Revision ID: 072_update_service_config_unique_constraint
Revises: 071_drop_infra_columns_from_services_mst
Create Date: 2025-12-15

This migration:
1. Drops the old unique constraint (uq_service_config_tenant_service_env_geo_loc_alb)
2. Creates new unique constraint including infrastructure fields:
   - tenant_mst_code
   - services_mst_code
   - environment
   - geo_loc_mst_code
   - alb_selection
   - infrastructuretype_ref_code
   - infrastructure_mst_code
   - infra_vendor_enum

This allows the same service to have separate configs for different infrastructure types (ECS vs EKS).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '072_update_service_config_unique_constraint'
down_revision: Union[str, None] = '071_drop_infra_columns_from_services_mst'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Drop old unique constraint
    op.drop_constraint(
        'uq_service_config_tenant_service_env_geo_loc_alb',
        'service_configs',
        type_='unique'
    )

    # Step 2: Create new unique constraint including infrastructure fields
    op.create_unique_constraint(
        'uq_service_config_tenant_service_env_geo_loc_alb_infra',
        'service_configs',
        [
            'tenant_mst_code',
            'services_mst_code',
            'environment',
            'geo_loc_mst_code',
            'alb_selection',
            'infrastructuretype_ref_code',
            'infrastructure_mst_code',
            'infra_vendor_enum'
        ]
    )


def downgrade() -> None:
    # Drop new constraint
    op.drop_constraint(
        'uq_service_config_tenant_service_env_geo_loc_alb_infra',
        'service_configs',
        type_='unique'
    )

    # Recreate old constraint
    op.create_unique_constraint(
        'uq_service_config_tenant_service_env_geo_loc_alb',
        'service_configs',
        ['tenant_mst_code', 'services_mst_code', 'environment', 'geo_loc_mst_code', 'alb_selection']
    )
