"""Drop PR tracking columns from transaction_queue

Revision ID: 094_drop_pr_tracking_columns_from_transaction_queue
Revises: 093_update_transaction_queue_to_new_structure
Create Date: 2026-01-07

Changes:
1. Drop columns: gitops_workflow_id, pr_number, pr_url, git_branch, commit_sha
2. These columns are no longer used in the GitopsQueueModel
3. PR tracking is now handled separately through the workflow system
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '094_drop_pr_tracking_columns_from_transaction_queue'
down_revision: Union[str, None] = '093_update_transaction_queue_to_new_structure'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop PR tracking columns (no longer used in model)
    op.drop_column('transaction_queue', 'gitops_workflow_id')
    op.drop_column('transaction_queue', 'pr_number')
    op.drop_column('transaction_queue', 'pr_url')
    op.drop_column('transaction_queue', 'git_branch')
    op.drop_column('transaction_queue', 'commit_sha')


def downgrade() -> None:
    # Restore PR tracking columns
    op.add_column(
        'transaction_queue',
        sa.Column('commit_sha', sa.String(64), nullable=True, comment='Commit SHA')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('git_branch', sa.String(200), nullable=True, comment='Feature branch name')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('pr_url', sa.String(500), nullable=True, comment='GitHub PR URL')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('pr_number', sa.Integer(), nullable=True, comment='GitHub PR number (populated after deploy)')
    )
    op.add_column(
        'transaction_queue',
        sa.Column('gitops_workflow_id', sa.BigInteger(), nullable=True, comment='FK to gitops_workflow_detail for PR tracking')
    )

    # Restore foreign key constraint
    op.create_foreign_key(
        'gitops_queue_gitops_workflow_id_fkey',
        'transaction_queue',
        'gitops_workflow_detail',
        ['gitops_workflow_id'],
        ['id'],
        ondelete='SET NULL'
    )
