"""create chat_history table

Revision ID: 096_create_chat_history_table
Revises: 095_create_transaction_queue_workflow_mapping_table
Create Date: 2026-01-08

Changes:
1. Create chat_history table for storing infra chat agent conversation history
2. Add LLM tracking fields (model, purpose, summary status)
3. Store message metadata (intent, resource, parameters)
4. Support placement and attribute parameters as separate JSONB fields
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '096_create_chat_history_table'
down_revision: Union[str, None] = '095_create_transaction_queue_workflow_mapping_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create chat_history table
    op.create_table(
        'chat_history',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), onupdate=sa.text('now()'), nullable=False),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),

        # Message content
        sa.Column('role', sa.String(20), nullable=False, comment='Message role: "user" or "agent"'),
        sa.Column('message', sa.Text(), nullable=False, comment='The actual message text'),

        # LLM tracking fields
        sa.Column('llm_model', sa.String(100), nullable=True, comment='LLM model used (e.g., gpt-4, claude-3-sonnet)'),
        sa.Column('llm_purpose', sa.String(100), nullable=True, comment='Purpose of LLM call (e.g., intent_detection, parameter_extraction)'),
        sa.Column('summary_status', sa.Boolean(), nullable=False, server_default='false', comment='Whether this message has been summarized'),

        # Metadata (optional fields for agent responses)
        sa.Column('intent', sa.String(50), nullable=True, comment='Detected intent: CREATE, REFERENCE, QA, UNSUPPORTED'),
        sa.Column('resource', sa.String(50), nullable=True, comment='Resource type for CREATE intent: s3, sqs, dynamodb, etc.'),
        sa.Column('placement_parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Placement parameters (environment, geo_loc, application, etc.)'),
        sa.Column('attribute_parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Attribute parameters (identifier, versioning, cross_account_id, etc.)'),
        sa.Column('is_ready', sa.Boolean(), nullable=False, server_default='false', comment='True when all required parameters are collected'),

        # Thread tracking
        sa.Column('thread_id', sa.String(200), nullable=True, comment='Thread identifier in format tenant_id:conversation_id'),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Create indexes for efficient lookups
    op.create_index('idx_chat_history_role', 'chat_history', ['role'])
    op.create_index('idx_chat_history_created_at', 'chat_history', ['created_at'])
    op.create_index('idx_chat_history_active', 'chat_history', ['is_active'])
    op.create_index('idx_chat_history_intent', 'chat_history', ['intent'])
    op.create_index('idx_chat_history_summary_status', 'chat_history', ['summary_status'])
    op.create_index('idx_chat_history_llm_model', 'chat_history', ['llm_model'])
    op.create_index('idx_chat_history_is_ready', 'chat_history', ['is_ready'])
    op.create_index('idx_chat_history_thread_id', 'chat_history', ['thread_id'])


def downgrade() -> None:
    # Drop indexes
    op.drop_index('idx_chat_history_thread_id', table_name='chat_history')
    op.drop_index('idx_chat_history_is_ready', table_name='chat_history')
    op.drop_index('idx_chat_history_llm_model', table_name='chat_history')
    op.drop_index('idx_chat_history_summary_status', table_name='chat_history')
    op.drop_index('idx_chat_history_intent', table_name='chat_history')
    op.drop_index('idx_chat_history_active', table_name='chat_history')
    op.drop_index('idx_chat_history_created_at', table_name='chat_history')
    op.drop_index('idx_chat_history_role', table_name='chat_history')

    # Drop table
    op.drop_table('chat_history')
