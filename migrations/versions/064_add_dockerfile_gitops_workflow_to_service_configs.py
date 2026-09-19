"""Add dockerfile_gitops_workflow_id to service_configs

Revision ID: 064_add_dockerfile_gitops_workflow_to_service_configs
Revises: 063_add_audit_triggers_to_all_tables
Create Date: 2025-12-09

Links service_configs to gitops_workflow_detail for Dockerfile PR tracking.
This is separate from the existing gitops_workflow_id which tracks Terragrunt PRs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '064_add_dockerfile_gitops_workflow_to_service_configs'
down_revision: Union[str, None] = '063_add_audit_triggers_to_all_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add dockerfile_gitops_workflow_id column to service_configs
    op.add_column(
        'service_configs',
        sa.Column(
            'dockerfile_gitops_workflow_id',
            sa.BigInteger(),
            nullable=True,
            comment='Foreign key to gitops_workflow_detail for Dockerfile PR tracking'
        )
    )

    # Create foreign key constraint
    op.create_foreign_key(
        'fk_service_configs_dockerfile_gitops_workflow',
        'service_configs',
        'gitops_workflow_detail',
        ['dockerfile_gitops_workflow_id'],
        ['id'],
        ondelete='SET NULL'
    )

    # Create index for better query performance
    op.create_index(
        'idx_service_configs_dockerfile_gitops_workflow',
        'service_configs',
        ['dockerfile_gitops_workflow_id']
    )


def downgrade() -> None:
    # Drop index
    op.drop_index('idx_service_configs_dockerfile_gitops_workflow', table_name='service_configs')

    # Drop foreign key constraint
    op.drop_constraint(
        'fk_service_configs_dockerfile_gitops_workflow',
        'service_configs',
        type_='foreignkey'
    )

    # Drop column
    op.drop_column('service_configs', 'dockerfile_gitops_workflow_id')
