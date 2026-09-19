"""Add gitops_workflow_id to service_configs

Revision ID: 055_add_gitops_workflow_to_service_configs
Revises: 054_remove_geo_loc_alb_from_sidecar_configs
Create Date: 2025-11-30

Links service_configs to gitops_workflow_detail for PR tracking.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '055_add_gitops_workflow_to_service_configs'
down_revision: Union[str, None] = '054_remove_geo_loc_alb_from_sidecar_configs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add gitops_workflow_id column to service_configs
    op.add_column(
        'service_configs',
        sa.Column(
            'gitops_workflow_id',
            sa.BigInteger(),
            nullable=True,
            comment='Foreign key to gitops_workflow_detail for PR tracking'
        )
    )

    # Create foreign key constraint
    op.create_foreign_key(
        'fk_service_configs_gitops_workflow',
        'service_configs',
        'gitops_workflow_detail',
        ['gitops_workflow_id'],
        ['id'],
        ondelete='SET NULL'
    )

    # Create index for better query performance
    op.create_index(
        'idx_service_configs_gitops_workflow',
        'service_configs',
        ['gitops_workflow_id']
    )


def downgrade() -> None:
    # Drop index
    op.drop_index('idx_service_configs_gitops_workflow', table_name='service_configs')

    # Drop foreign key constraint
    op.drop_constraint(
        'fk_service_configs_gitops_workflow',
        'service_configs',
        type_='foreignkey'
    )

    # Drop column
    op.drop_column('service_configs', 'gitops_workflow_id')
