"""Add sync_status to service_configs

Revision ID: 062_add_sync_status_to_service_configs
Revises: 061_add_tenant_and_user_to_gitops_workflow_detail
Create Date: 2025-12-03

Adds sync_status column to track whether config is synced to GitHub.
Values: NEVER_SYNCED, PENDING_SYNC, SYNCED
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '062_add_sync_status_to_service_configs'
down_revision: Union[str, None] = '061_add_tenant_and_user_to_gitops_workflow_detail'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add sync_status column to service_configs
    op.add_column(
        'service_configs',
        sa.Column(
            'sync_status',
            sa.String(20),
            nullable=False,
            server_default='SYNCED',  # Existing records were created with auto-sync
            comment='Sync status: NEVER_SYNCED, PENDING_SYNC, SYNCED'
        )
    )

    # Create index for better query performance when filtering by sync status
    op.create_index(
        'idx_service_configs_sync_status',
        'service_configs',
        ['sync_status']
    )


def downgrade() -> None:
    # Drop index
    op.drop_index('idx_service_configs_sync_status', table_name='service_configs')

    # Drop column
    op.drop_column('service_configs', 'sync_status')
