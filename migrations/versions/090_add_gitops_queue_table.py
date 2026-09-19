"""Add gitops_queue table for batch deployment queue

Revision ID: 090_add_gitops_queue_table
Revises: 089_add_ops_tools_service_type
Create Date: 2026-01-02

Stores items queued for batch deployment via GitOps workflow.
Supports "Add to Queue" -> "Deploy All" pattern for creating single PRs
with multiple infrastructure/service config changes.

Status Lifecycle:
    pending -> pr_raised -> pr_merged/pr_closed
    pending -> deleted (removed before deploy)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '090_add_gitops_queue_table'
down_revision: Union[str, None] = '089_add_ops_tools_service_type'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the status enum type
    gitops_queue_status_enum = postgresql.ENUM(
        'pending', 'deleted', 'pr_raised', 'pr_merged', 'pr_closed',
        name='gitops_queue_status_enum',
        create_type=False
    )

    # Create enum type first
    op.execute("CREATE TYPE gitops_queue_status_enum AS ENUM ('pending', 'deleted', 'pr_raised', 'pr_merged', 'pr_closed')")

    # Create the gitops_queue table
    op.create_table(
        'gitops_queue',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_code', sa.String(100), nullable=False, comment='User who added this item to queue'),
        sa.Column('service_config_code', sa.String(100), nullable=True, comment='Service config code (nullable for standalone infra)'),
        sa.Column('environment', sa.String(50), nullable=False, comment='Environment: dev, staging, prod'),
        sa.Column('infra_type', sa.String(50), nullable=False, comment='Infrastructure type: ecs, s3, sqs, dynamodb, etc.'),
        sa.Column('config_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='Complete config as JSON at time of adding to queue'),
        sa.Column('hcl_file_path', sa.String(500), nullable=False, comment='HCL file path in repo'),
        sa.Column('atlantis_project_name', sa.String(200), nullable=False, comment='Atlantis project name'),
        sa.Column('status', gitops_queue_status_enum, nullable=False, server_default='pending', comment='Status: pending, deleted, pr_raised, pr_merged, pr_closed'),
        sa.Column('gitops_workflow_id', sa.BigInteger(), nullable=True, comment='FK to gitops_workflow_detail for PR tracking'),
        sa.Column('pr_number', sa.Integer(), nullable=True, comment='GitHub PR number (populated after deploy)'),
        sa.Column('pr_url', sa.String(500), nullable=True, comment='GitHub PR URL'),
        sa.Column('git_branch', sa.String(200), nullable=True, comment='Feature branch name'),
        sa.Column('commit_sha', sa.String(64), nullable=True, comment='Commit SHA'),
        sa.Column('tenant_code', sa.String(100), nullable=True, comment='Tenant code'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='When item was added to queue'),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Last update time'),
        sa.Column('deleted_at', sa.TIMESTAMP(timezone=True), nullable=True, comment='Soft delete timestamp'),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_code'], ['user_mst.code'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['gitops_workflow_id'], ['gitops_workflow_detail.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_code'], ['tenants_mst.code'], ondelete='CASCADE'),
    )

    # Create indexes
    op.create_index('idx_gitops_queue_user_code', 'gitops_queue', ['user_code'])
    op.create_index('idx_gitops_queue_service_config_code', 'gitops_queue', ['service_config_code'])
    op.create_index('idx_gitops_queue_pr_number', 'gitops_queue', ['pr_number'])
    op.create_index('idx_gitops_queue_gitops_workflow_id', 'gitops_queue', ['gitops_workflow_id'])
    op.create_index('idx_gitops_queue_tenant_code', 'gitops_queue', ['tenant_code'])

    # Composite indexes for common queries
    op.create_index('idx_gitops_queue_user_status', 'gitops_queue', ['user_code', 'status'])
    op.create_index('idx_gitops_queue_pr', 'gitops_queue', ['pr_number', 'status'])
    op.create_index('idx_gitops_queue_tenant', 'gitops_queue', ['tenant_code', 'status'])


def downgrade() -> None:
    # Drop indexes
    op.drop_index('idx_gitops_queue_tenant', table_name='gitops_queue')
    op.drop_index('idx_gitops_queue_pr', table_name='gitops_queue')
    op.drop_index('idx_gitops_queue_user_status', table_name='gitops_queue')
    op.drop_index('idx_gitops_queue_tenant_code', table_name='gitops_queue')
    op.drop_index('idx_gitops_queue_gitops_workflow_id', table_name='gitops_queue')
    op.drop_index('idx_gitops_queue_pr_number', table_name='gitops_queue')
    op.drop_index('idx_gitops_queue_service_config_code', table_name='gitops_queue')
    op.drop_index('idx_gitops_queue_user_code', table_name='gitops_queue')

    # Drop table
    op.drop_table('gitops_queue')

    # Drop enum type
    op.execute("DROP TYPE IF EXISTS gitops_queue_status_enum")
