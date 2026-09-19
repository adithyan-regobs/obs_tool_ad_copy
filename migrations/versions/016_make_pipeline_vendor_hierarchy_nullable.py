"""Make pipeline_vendor_mst hierarchy columns nullable

Revision ID: 016_pipeline_vendor_nullable
Revises: 015_final_cleanup
Create Date: 2025-11-05 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '016_pipeline_vendor_nullable'
down_revision: Union[str, None] = '015_final_cleanup'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Make ALL hierarchy columns nullable in pipeline_vendor_mst table.

    This supports the hierarchical configuration lookup pattern where:
    - Global-level configs: ALL hierarchy columns NULL (system-wide defaults)
    - Tenant-level configs: only tenants_mst_code set, others NULL
    - Application-level configs: applications_mst_code set, others NULL
    - Resource Group-level configs: resource_group_mst_code set, others NULL
    - Service-level configs: service_mst_code set
    """

    # Make tenants_mst_code nullable (for global defaults)
    op.alter_column(
        'pipeline_vendor_mst',
        'tenants_mst_code',
        existing_type=sa.String(length=100),
        nullable=True
    )

    # Make service_mst_code nullable
    op.alter_column(
        'pipeline_vendor_mst',
        'service_mst_code',
        existing_type=sa.String(length=100),
        nullable=True
    )

    # Make applications_mst_code nullable
    op.alter_column(
        'pipeline_vendor_mst',
        'applications_mst_code',
        existing_type=sa.String(length=100),
        nullable=True
    )

    # Make resource_group_mst_code nullable
    op.alter_column(
        'pipeline_vendor_mst',
        'resource_group_mst_code',
        existing_type=sa.String(length=100),
        nullable=True
    )


def downgrade() -> None:
    """
    Revert ALL hierarchy columns to NOT NULL.

    WARNING: This will fail if there are any NULL values in these columns.
    You should populate all NULL values before running this downgrade.
    """

    # Make resource_group_mst_code NOT NULL
    op.alter_column(
        'pipeline_vendor_mst',
        'resource_group_mst_code',
        existing_type=sa.String(length=100),
        nullable=False
    )

    # Make applications_mst_code NOT NULL
    op.alter_column(
        'pipeline_vendor_mst',
        'applications_mst_code',
        existing_type=sa.String(length=100),
        nullable=False
    )

    # Make service_mst_code NOT NULL
    op.alter_column(
        'pipeline_vendor_mst',
        'service_mst_code',
        existing_type=sa.String(length=100),
        nullable=False
    )

    # Make tenants_mst_code NOT NULL
    op.alter_column(
        'pipeline_vendor_mst',
        'tenants_mst_code',
        existing_type=sa.String(length=100),
        nullable=False
    )
