"""Add chat_message table

Revision ID: 002_chat_message
Revises: 001_chat_info
Create Date: 2025-11-04 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '002_chat_message'
down_revision: Union[str, None] = '001_chat_info'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create chat_message table"""
    op.create_table(
        'chat_message',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('chat_info_code', sa.String(length=100), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('summary_status', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
        sa.ForeignKeyConstraint(['chat_info_code'], ['chat_info.code'], ondelete='CASCADE'),
    )

    # Create indexes for better query performance
    op.create_index('idx_chat_message_chat_info', 'chat_message', ['chat_info_code'])
    op.create_index('idx_chat_message_role', 'chat_message', ['role'])
    op.create_index('idx_chat_message_summary_status', 'chat_message', ['summary_status'])
    op.create_index('idx_chat_message_created_at', 'chat_message', ['created_at'])
    op.create_index('idx_chat_message_active', 'chat_message', ['is_active'])


def downgrade() -> None:
    """Drop chat_message table"""
    op.drop_index('idx_chat_message_active', table_name='chat_message')
    op.drop_index('idx_chat_message_created_at', table_name='chat_message')
    op.drop_index('idx_chat_message_summary_status', table_name='chat_message')
    op.drop_index('idx_chat_message_role', table_name='chat_message')
    op.drop_index('idx_chat_message_chat_info', table_name='chat_message')
    op.drop_table('chat_message')