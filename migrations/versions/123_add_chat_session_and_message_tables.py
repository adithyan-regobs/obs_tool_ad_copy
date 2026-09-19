"""Add chat_session_model and conversation_message_model tables

Revision ID: 123_add_chat_session_and_message_tables
Revises: 122_add_log_provider_config
Create Date: 2026-04-06

Adds chat_session_model table for tracking chat sessions with ticket/tenant
context and collected form data, and conversation_message_model table for
storing individual messages within a session.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "123_add_chat_session_and_message_tables"
down_revision: Union[str, None] = "122_add_log_provider_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_session_model (
            id BIGSERIAL PRIMARY KEY,
            code VARCHAR(100) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            description VARCHAR(500),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            is_deleted BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,

            ticket_code VARCHAR(100) NOT NULL,
            tenant_code VARCHAR(100) NOT NULL,
            user_mst_code VARCHAR(255) NOT NULL,
            form_id VARCHAR(100),
            status VARCHAR(50) NOT NULL DEFAULT 'pending',
            collected_data JSONB NOT NULL DEFAULT '{}',
            currently_asking VARCHAR(100),
            skipped_fields JSONB NOT NULL DEFAULT '[]'::jsonb
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_chatsession_ticket ON chat_session_model (ticket_code)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_chatsession_user ON chat_session_model (user_mst_code)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS conversation_message_model (
            id BIGSERIAL PRIMARY KEY,
            code VARCHAR(100) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            description VARCHAR(500),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            is_deleted BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,

            session_id BIGINT NOT NULL REFERENCES chat_session_model(id),
            role VARCHAR(20) NOT NULL,
            message TEXT NOT NULL
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_conversationmsg_session ON conversation_message_model (session_id)")


def downgrade() -> None:
    op.drop_table("conversation_message_model")
    op.drop_table("chat_session_model")
