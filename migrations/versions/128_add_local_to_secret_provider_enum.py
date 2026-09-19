"""Add 'local' value to secret_provider_enum

Revision ID: 128_add_local_to_secret_provider_enum
Revises: 127_add_last_suggestion_map_to_chat_session
Create Date: 2026-04-18

Adds LOCAL = 'local' to secret_provider_enum for VARIABLE-type records
where the value is stored directly in the database (no external secret backend).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "128_add_local_to_secret_provider_enum"
down_revision: Union[str, None] = "127_add_last_suggestion_map_to_chat_session"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE secret_provider_enum ADD VALUE IF NOT EXISTS 'local';")


def downgrade() -> None:
    # Postgres does not support removing enum values directly.
    # To roll back, recreate the type without 'local' and migrate existing rows.
    pass
