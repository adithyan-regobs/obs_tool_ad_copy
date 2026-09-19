"""Add pr_status to gitops_workflow_detail

Revision ID: 060_add_pr_status_to_gitops_workflow_detail
Revises: 059_delete_non_mumbai_geo_loc_mst
Create Date: 2025-12-01

Adds pr_status column to track PR state: PR_OPEN, PR_MERGED, PR_CLOSED.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '060_add_pr_status_to_gitops_workflow_detail'
down_revision: Union[str, None] = '059_delete_non_mumbai_geo_loc_mst'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the enum type first
    pr_status_enum = sa.Enum('PR_OPEN', 'PR_MERGED', 'PR_CLOSED', name='pr_status_enum')
    pr_status_enum.create(op.get_bind(), checkfirst=True)

    # Add pr_status column to gitops_workflow_detail
    op.add_column(
        'gitops_workflow_detail',
        sa.Column(
            'pr_status',
            pr_status_enum,
            nullable=True,
            server_default='PR_OPEN',
            comment='PR status: PR_OPEN, PR_MERGED, PR_CLOSED'
        )
    )


def downgrade() -> None:
    # Drop column
    op.drop_column('gitops_workflow_detail', 'pr_status')

    # Drop enum type
    pr_status_enum = sa.Enum('PR_OPEN', 'PR_MERGED', 'PR_CLOSED', name='pr_status_enum')
    pr_status_enum.drop(op.get_bind(), checkfirst=True)
