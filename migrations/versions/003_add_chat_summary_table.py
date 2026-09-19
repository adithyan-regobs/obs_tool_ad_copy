"""Add chat_summary table

Revision ID: 003_chat_summary
Revises: 002_chat_message
Create Date: 2025-11-04 18:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '003_chat_summary'
down_revision: Union[str, None] = '002_chat_message'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create chat_summary table"""
    op.create_table(
        'chat_summary',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('chat_info_code', sa.String(length=100), nullable=False),
        sa.Column('summary_text', sa.Text(), nullable=False),
        sa.Column('message_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
        sa.ForeignKeyConstraint(['chat_info_code'], ['chat_info.code'], ondelete='CASCADE'),
    )

    # Create indexes for better query performance
    op.create_index('idx_chat_summary_chat_info', 'chat_summary', ['chat_info_code'])
    op.create_index('idx_chat_summary_message_count', 'chat_summary', ['message_count'])
    op.create_index('idx_chat_summary_created_at', 'chat_summary', ['created_at'])
    op.create_index('idx_chat_summary_updated_at', 'chat_summary', ['updated_at'])


def downgrade() -> None:
    """Drop chat_summary table"""
    op.drop_index('idx_chat_summary_updated_at', table_name='chat_summary')
    op.drop_index('idx_chat_summary_created_at', table_name='chat_summary')
    op.drop_index('idx_chat_summary_message_count', table_name='chat_summary')
    op.drop_index('idx_chat_summary_chat_info', table_name='chat_summary')
    op.drop_table('chat_summary')
