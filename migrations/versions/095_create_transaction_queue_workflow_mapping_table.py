"""create transaction_queue_workflow_mapping table

Revision ID: 095_create_transaction_queue_workflow_mapping_table
Revises: 094_drop_pr_tracking_columns_from_transaction_queue
Create Date: 2026-01-07

Changes:
1. Create transaction_queue_workflow_mapping table
2. Add foreign keys to transaction_queue and gitops_workflow_detail
3. Add composite indexes for efficient lookups
4. Enable bidirectional relationships between transaction_queue and gitops_workflow_detail
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '095_create_transaction_queue_workflow_mapping_table'
down_revision: Union[str, None] = '094_drop_pr_tracking_columns_from_transaction_queue'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create transaction_queue_workflow_mapping table
    op.create_table(
        'transaction_queue_workflow_mapping',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('transaction_queue_code', sa.String(100), nullable=False, comment='Foreign key reference to transaction_queue.code'),
        sa.Column('gitops_workflow_code', sa.String(100), nullable=False, comment='Foreign key reference to gitops_workflow_detail.code'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), onupdate=sa.text('now()'), nullable=False),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Create foreign key constraints (using code instead of id)
    op.create_foreign_key(
        'transaction_queue_workflow_mapping_queue_fkey',
        'transaction_queue_workflow_mapping',
        'transaction_queue',
        ['transaction_queue_code'],
        ['code'],
        ondelete='CASCADE'
    )

    op.create_foreign_key(
        'transaction_queue_workflow_mapping_workflow_fkey',
        'transaction_queue_workflow_mapping',
        'gitops_workflow_detail',
        ['gitops_workflow_code'],
        ['code'],
        ondelete='CASCADE'
    )

    # Create indexes
    op.create_index('idx_transaction_queue_workflow_mapping_queue_code', 'transaction_queue_workflow_mapping', ['transaction_queue_code'])
    op.create_index('idx_transaction_queue_workflow_mapping_workflow_code', 'transaction_queue_workflow_mapping', ['gitops_workflow_code'])
    op.create_index('idx_transaction_queue_workflow_mapping_queue_workflow', 'transaction_queue_workflow_mapping', ['transaction_queue_code', 'gitops_workflow_code'])
    op.create_index('idx_transaction_queue_workflow_mapping_workflow_queue', 'transaction_queue_workflow_mapping', ['gitops_workflow_code', 'transaction_queue_code'])


def downgrade() -> None:
    # Drop indexes
    op.drop_index('idx_transaction_queue_workflow_mapping_workflow_queue', table_name='transaction_queue_workflow_mapping')
    op.drop_index('idx_transaction_queue_workflow_mapping_queue_workflow', table_name='transaction_queue_workflow_mapping')
    op.drop_index('idx_transaction_queue_workflow_mapping_workflow_code', table_name='transaction_queue_workflow_mapping')
    op.drop_index('idx_transaction_queue_workflow_mapping_queue_code', table_name='transaction_queue_workflow_mapping')

    # Drop foreign key constraints
    op.drop_constraint('transaction_queue_workflow_mapping_workflow_fkey', 'transaction_queue_workflow_mapping', type_='foreignkey')
    op.drop_constraint('transaction_queue_workflow_mapping_queue_fkey', 'transaction_queue_workflow_mapping', type_='foreignkey')

    # Drop table
    op.drop_table('transaction_queue_workflow_mapping')
