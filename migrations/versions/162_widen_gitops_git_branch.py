"""widen gitops_workflow_detail.git_branch to 255

Revision ID: 162_widen_gitops_git_branch
Revises: 161_add_sync_tool_records
Create Date: 2026-08-16

Generated feature branch names are capped at 255 chars (git/GitHub's limit)
by the file locators, but the column only held 100. A 144-char name — from a
generated branch being reused as a base branch and embedded into the next
name — failed the INSERT with StringDataRightTruncationError and took the
whole create-PR run down. 255 matches the generator's cap and BaseModel.name,
which stores "PR Workflow - {branch}".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '162_widen_gitops_git_branch'
down_revision: Union[str, None] = '161_add_sync_tool_records'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'gitops_workflow_detail',
        'git_branch',
        existing_type=sa.String(100),
        type_=sa.String(255),
        existing_nullable=True,
        existing_comment='Feature branch name',
    )


def downgrade() -> None:
    op.alter_column(
        'gitops_workflow_detail',
        'git_branch',
        existing_type=sa.String(255),
        type_=sa.String(100),
        existing_nullable=True,
        existing_comment='Feature branch name',
    )
