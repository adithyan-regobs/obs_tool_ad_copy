"""widen remaining branch columns to 255

Revision ID: 163_widen_branch_columns
Revises: 162_widen_gitops_git_branch
Create Date: 2026-08-16

Completes what 162 started for gitops_workflow_detail.git_branch: every other
column that stores a branch name was still varchar(100), while branch names
can legitimately reach 255 (git/GitHub's cap, matched by the file locators'
generator). Both of these store the BASE branch — the branch selected on the
service — so a long generated branch chosen as base overflows them on every
PR/deploy run (pipeline_mst.repo_branch failed at 202 chars in prod).
Separate revision because 162 was already applied to prod when this surfaced;
extending an applied revision is silently skipped by alembic.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '163_widen_branch_columns'
down_revision: Union[str, None] = '162_widen_gitops_git_branch'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'pipeline_mst',
        'repo_branch',
        existing_type=sa.String(100),
        type_=sa.String(255),
        existing_nullable=False,
    )
    op.alter_column(
        'service_config_dockerfile_workflows',
        'branch',
        existing_type=sa.String(100),
        type_=sa.String(255),
        existing_nullable=False,
        existing_comment='Base branch name (main, stage, etc.)',
    )

def downgrade() -> None:
    op.alter_column(
        'service_config_dockerfile_workflows',
        'branch',
        existing_type=sa.String(255),
        type_=sa.String(100),
        existing_nullable=False,
        existing_comment='Base branch name (main, stage, etc.)',
    )
    op.alter_column(
        'pipeline_mst',
        'repo_branch',
        existing_type=sa.String(255),
        type_=sa.String(100),
        existing_nullable=False,
    )
