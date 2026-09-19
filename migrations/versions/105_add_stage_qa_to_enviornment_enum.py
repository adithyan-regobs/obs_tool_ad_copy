"""Add stage and qa to enviornment_enum

Revision ID: 105_add_stage_qa_to_enviornment_enum
Revises: 104_add_pr_merged_to_transaction_queue_status
Create Date: 2026-02-13

Adds 'stage' and 'qa' values to the enviornment_enum PostgreSQL enum type
so that infrastructure_mst and infra_vendor_accounts_mst can use them.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '105_add_stage_qa_to_enviornment_enum'
down_revision: Union[str, None] = '104_add_pr_merged_to_transaction_queue_status'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE enviornment_enum ADD VALUE IF NOT EXISTS 'stage'"
    )
    op.execute(
        "ALTER TYPE enviornment_enum ADD VALUE IF NOT EXISTS 'qa'"
    )


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values directly.
    pass
