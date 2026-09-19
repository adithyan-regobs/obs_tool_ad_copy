"""add slack integration tables and columns

Revision ID: 083_add_slack_integration_tables
Revises: 082_add_namespace_mst_table
Create Date: 2025-12-23

Adds Slack integration support for InfraStudio:
- New table: slack_conversation_state (tracks conversation context and pending actions)
- Alter chat_info: add slack_channel_id, slack_thread_ts, source columns
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP, JSON

# revision identifiers
revision = '083_add_slack_integration_tables'
down_revision = '082_add_namespace_mst_table'
branch_labels = None
depends_on = None


def upgrade():
    # ============================================================
    # CREATE TABLE: slack_conversation_state
    # ============================================================
    op.create_table(
        'slack_conversation_state',

        # Primary columns
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('slack_channel_id', sa.String(20), nullable=False),
        sa.Column('slack_thread_ts', sa.String(20), nullable=False),
        sa.Column('chat_info_code', sa.String(100), nullable=True),
        sa.Column('context', JSON, nullable=True),
        sa.Column('pending_selection_type', sa.String(50), nullable=True),
        sa.Column('pending_deployment', JSON, nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', TIMESTAMP(timezone=True), nullable=True),

        # Constraints
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slack_channel_id', 'slack_thread_ts',
                           name='uq_slack_conversation_channel_thread'),
        sa.ForeignKeyConstraint(
            ['chat_info_code'],
            ['chat_info.code'],
            name='fk_slack_conversation_chat_info',
            ondelete='SET NULL'
        )
    )

    # Indexes for slack_conversation_state
    op.create_index(
        'idx_slack_conversation_channel',
        'slack_conversation_state',
        ['slack_channel_id']
    )
    op.create_index(
        'idx_slack_conversation_thread',
        'slack_conversation_state',
        ['slack_thread_ts']
    )
    op.create_index(
        'idx_slack_conversation_expires',
        'slack_conversation_state',
        ['expires_at']
    )
    op.create_index(
        'idx_slack_conversation_chat_info',
        'slack_conversation_state',
        ['chat_info_code']
    )

    # ============================================================
    # ALTER TABLE: chat_info (add Slack tracking columns)
    # ============================================================
    op.add_column(
        'chat_info',
        sa.Column('slack_channel_id', sa.String(20), nullable=True)
    )
    op.add_column(
        'chat_info',
        sa.Column('slack_thread_ts', sa.String(20), nullable=True)
    )
    op.add_column(
        'chat_info',
        sa.Column('source', sa.String(20), server_default='web', nullable=True)
    )

    # Index for finding Slack threads
    op.create_index(
        'idx_chat_info_slack_thread',
        'chat_info',
        ['slack_channel_id', 'slack_thread_ts']
    )

    # Note: Deployment tracking columns (source, slack_user_id, slack_channel_id)
    # can be added to infrastructure_mst table in a future migration if needed for audit


def downgrade():
    # ============================================================
    # DOWNGRADE: chat_info
    # ============================================================
    op.drop_index('idx_chat_info_slack_thread', table_name='chat_info')
    op.drop_column('chat_info', 'source')
    op.drop_column('chat_info', 'slack_thread_ts')
    op.drop_column('chat_info', 'slack_channel_id')

    # ============================================================
    # DOWNGRADE: slack_conversation_state
    # ============================================================
    op.drop_index('idx_slack_conversation_chat_info', table_name='slack_conversation_state')
    op.drop_index('idx_slack_conversation_expires', table_name='slack_conversation_state')
    op.drop_index('idx_slack_conversation_thread', table_name='slack_conversation_state')
    op.drop_index('idx_slack_conversation_channel', table_name='slack_conversation_state')
    op.drop_table('slack_conversation_state')
