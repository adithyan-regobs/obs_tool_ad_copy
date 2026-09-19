"""Rename gitops_queue to transaction_queue and add code column

Revision ID: 091_rename_gitops_queue_to_transaction_queue_and_add_code
Revises: 090_add_gitops_queue_table
Create Date: 2026-01-04

1. Renames gitops_queue table to transaction_queue
2. Adds a unique 'code' column to be used as QueueID in API calls
This provides a more stable identifier than the auto-increment ID.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '091_rename_gitops_queue_to_transaction_queue_and_add_code'
down_revision: Union[str, None] = '090_add_gitops_queue_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Rename table from gitops_queue to transaction_queue
    op.execute("ALTER TABLE gitops_queue RENAME TO transaction_queue")

    # Step 2: Add code column as nullable first
    op.add_column(
        'transaction_queue',
        sa.Column('code', sa.String(100), nullable=True, comment='Unique queue identifier code (QueueID)')
    )

    # Step 3: Create unique index on code column
    op.create_index(
        'idx_transaction_queue_code',
        'transaction_queue',
        ['code'],
        unique=True
    )

    # Step 4: Populate code column with values based on id
    # Format: QUEUE-{id} e.g., QUEUE-1, QUEUE-2, etc.
    op.execute("""
        UPDATE transaction_queue
        SET code = 'QUEUE-' || id::text
        WHERE code IS NULL
    """)

    # Step 5: Now make the column NOT NULL
    op.alter_column(
        'transaction_queue',
        'code',
        nullable=False
    )

    # Step 6: Rename indexes (gitops_queue_* -> transaction_queue_*)
    op.execute("ALTER INDEX idx_gitops_queue_user_status RENAME TO idx_transaction_queue_user_status")
    op.execute("ALTER INDEX idx_gitops_queue_pr RENAME TO idx_transaction_queue_pr")
    op.execute("ALTER INDEX idx_gitops_queue_tenant RENAME TO idx_transaction_queue_tenant")


def downgrade() -> None:
    # Reverse the changes

    # Step 1: Rename indexes back
    op.execute("ALTER INDEX idx_transaction_queue_user_status RENAME TO idx_gitops_queue_user_status")
    op.execute("ALTER INDEX idx_transaction_queue_pr RENAME TO idx_gitops_queue_pr")
    op.execute("ALTER INDEX idx_transaction_queue_tenant RENAME TO idx_gitops_queue_tenant")

    # Step 2: Make code column nullable
    op.alter_column(
        'transaction_queue',
        'code',
        nullable=True
    )

    # Step 3: Drop code column
    op.drop_index('idx_transaction_queue_code', table_name='transaction_queue')
    op.drop_column('transaction_queue', 'code')

    # Step 4: Rename table back
    op.execute("ALTER TABLE transaction_queue RENAME TO gitops_queue")
