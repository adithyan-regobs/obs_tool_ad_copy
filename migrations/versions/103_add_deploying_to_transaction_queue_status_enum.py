"""Add DEPLOYING to transaction_queue_status_enum

Revision ID: 103_add_deploying_to_transaction_queue_status_enum
Revises: 102_add_ticket_code_to_transaction_queue
Create Date: 2026-01-17

Adds DEPLOYING value to the transaction_queue_status_enum PostgreSQL enum type.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '103_add_deploying_to_transaction_queue_status_enum'
down_revision: Union[str, None] = '102_add_ticket_code_to_transaction_queue'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS 'deploying'"
    )


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values directly.
    # For safety, we leave this as a no-op.
    pass
