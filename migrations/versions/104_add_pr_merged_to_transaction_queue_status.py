"""Add pr_merged to transaction_queue_status_enum

Revision ID: 104_add_pr_merged_to_transaction_queue_status
Revises: 103_add_deploying_to_transaction_queue_status_enum
Create Date: 2026-01-31

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '104_add_pr_merged_to_transaction_queue_status'
down_revision: Union[str, None] = '103_add_deploying_to_transaction_queue_status_enum'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS 'pr_merged'")


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values directly
    pass
