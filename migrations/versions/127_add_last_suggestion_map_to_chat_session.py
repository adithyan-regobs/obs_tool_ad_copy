"""Add last_suggestion_map column to chat_session_model

Revision ID: 127_add_last_suggestion_map_to_chat_session
Revises: 126_add_model_registry
Create Date: 2026-04-11

Adds a JSONB column last_suggestion_map to chat_session_model for storing
the most recent suggestion map surfaced to the user within a chat session.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "127_add_last_suggestion_map_to_chat_session"
down_revision: Union[str, None] = "126_add_model_registry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE chat_session_model
        ADD COLUMN IF NOT EXISTS last_suggestion_map JSONB NOT NULL DEFAULT '{}'
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE chat_session_model
        DROP COLUMN IF EXISTS last_suggestion_map
    """)
