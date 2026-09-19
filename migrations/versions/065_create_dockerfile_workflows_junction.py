"""Create junction table for service_config dockerfile workflows

Revision ID: 065_create_dockerfile_workflows_junction
Revises: 064_add_dockerfile_gitops_workflow_to_service_configs
Create Date: 2025-12-10

Creates a junction table to link service_configs to gitops_workflow_detail
for Dockerfile PRs. This allows multiple PRs (one per branch per repository) per service config.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '065_create_dockerfile_workflows_junction'
down_revision: Union[str, None] = '064_add_dockerfile_gitops_workflow_to_service_configs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create junction table
    op.create_table(
        'service_config_dockerfile_workflows',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('service_config_id', sa.BigInteger(), nullable=False),
        sa.Column('gitops_workflow_id', sa.BigInteger(), nullable=False),
        sa.Column('branch', sa.String(100), nullable=False, comment='Base branch name (main, stage, etc.)'),
        sa.Column('repository', sa.String(200), nullable=False, comment='GitHub repository (owner/repo)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now(), nullable=True),

        # Primary key
        sa.PrimaryKeyConstraint('id'),

        # Foreign keys
        sa.ForeignKeyConstraint(
            ['service_config_id'],
            ['service_configs.id'],
            name='fk_scdw_service_config',
            ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['gitops_workflow_id'],
            ['gitops_workflow_detail.id'],
            name='fk_scdw_gitops_workflow',
            ondelete='CASCADE'
        ),

        # Unique constraint: one workflow per branch per repository per config
        sa.UniqueConstraint('service_config_id', 'branch', 'repository', name='uq_scdw_config_branch_repo'),

        comment='Junction table linking service_configs to gitops_workflow_detail for Dockerfile PRs'
    )

    # Create indexes for better query performance
    op.create_index(
        'idx_scdw_service_config_id',
        'service_config_dockerfile_workflows',
        ['service_config_id']
    )
    op.create_index(
        'idx_scdw_gitops_workflow_id',
        'service_config_dockerfile_workflows',
        ['gitops_workflow_id']
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index('idx_scdw_gitops_workflow_id', table_name='service_config_dockerfile_workflows')
    op.drop_index('idx_scdw_service_config_id', table_name='service_config_dockerfile_workflows')

    # Drop table
    op.drop_table('service_config_dockerfile_workflows')
