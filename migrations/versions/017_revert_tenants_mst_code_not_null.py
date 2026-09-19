"""Revert tenants_mst_code to NOT NULL

Revision ID: 017_tenants_not_null
Revises: 016_pipeline_vendor_nullable
Create Date: 2025-11-05 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '017_tenants_not_null'
down_revision: Union[str, None] = '016_pipeline_vendor_nullable'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Make tenants_mst_code NOT NULL in pipeline_vendor_mst table.

    This enforces tenant isolation - every pipeline vendor configuration
    must belong to a specific tenant. The hierarchical pattern still works
    within each tenant:

    - Tenant-level: only tenants_mst_code set, other hierarchy columns NULL
    - Application-level: tenants_mst_code + applications_mst_code set
    - Resource Group-level: tenants_mst_code + resource_group_mst_code set
    - Service-level: tenants_mst_code + service_mst_code set

    WARNING: This will fail if there are any rows with NULL tenants_mst_code.
    """

    # Make tenants_mst_code NOT NULL
    op.alter_column(
        'pipeline_vendor_mst',
        'tenants_mst_code',
        existing_type=sa.String(length=100),
        nullable=False
    )


def downgrade() -> None:
    """
    Revert tenants_mst_code to nullable.

    This allows global-level configurations (all hierarchy columns NULL).
    """

    # Make tenants_mst_code nullable
    op.alter_column(
        'pipeline_vendor_mst',
        'tenants_mst_code',
        existing_type=sa.String(length=100),
        nullable=True
    )
