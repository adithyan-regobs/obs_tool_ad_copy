"""Add api_dropdown_cache column to chat_session_model

Revision ID: 124_add_api_dropdown_cache_to_chat_session
Revises: 123_add_chat_session_and_message_tables
Create Date: 2026-04-07

Adds a JSONB column api_dropdown_cache to chat_session_model for caching
API-driven dropdown options within a chat session.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "124_add_api_dropdown_cache_to_chat_session"
down_revision: Union[str, None] = "123_add_chat_session_and_message_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE chat_session_model
        ADD COLUMN IF NOT EXISTS api_dropdown_cache JSONB NOT NULL DEFAULT '{}'
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE chat_session_model
        DROP COLUMN IF EXISTS api_dropdown_cache
    """)
