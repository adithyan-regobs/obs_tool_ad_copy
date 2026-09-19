"""add commit tracking columns to pipeline_run_track

Revision ID: 9b8a9dca40f8
Revises: 018_infra_mst_fk
Create Date: 2025-11-05 06:34:45.943698

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b8a9dca40f8'
down_revision: Union[str, Sequence[str], None] = '018_infra_mst_fk'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add commit_sha column
    op.add_column(
        'pipeline_run_track',
        sa.Column('commit_sha', sa.String(length=100), nullable=True)
    )

    # Add github_run_id column
    op.add_column(
        'pipeline_run_track',
        sa.Column('github_run_id', sa.String(length=100), nullable=True)
    )

    # Add error_message column
    op.add_column(
        'pipeline_run_track',
        sa.Column('error_message', sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop columns in reverse order
    op.drop_column('pipeline_run_track', 'error_message')
    op.drop_column('pipeline_run_track', 'github_run_id')
    op.drop_column('pipeline_run_track', 'commit_sha')
