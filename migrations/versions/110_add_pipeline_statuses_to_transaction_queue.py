"""Add pipeline statuses to transaction_queue_status_enum

Revision ID: 110_add_pipeline_statuses_to_transaction_queue
Revises: 109_add_jenkins_to_pipeline_agent_enum
Create Date: 2026-03-11

Adds 'provisioning', 'building', 'deployed', 'failed' values to the
transaction_queue_status_enum for platform-managed CI pipeline tracking.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '110_add_pipeline_statuses_to_transaction_queue'
down_revision: Union[str, None] = '109_add_jenkins_to_pipeline_agent_enum'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS 'provisioning'"
    )
    op.execute(
        "ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS 'building'"
    )
    op.execute(
        "ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS 'deployed'"
    )
    op.execute(
        "ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS 'failed'"
    )


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values directly.
    pass
