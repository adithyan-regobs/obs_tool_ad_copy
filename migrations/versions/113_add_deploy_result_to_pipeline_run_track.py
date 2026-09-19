"""Add deploy_result column to pipeline_run_track

Revision ID: 113_add_deploy_result_to_pipeline_run_track
Revises: 112_add_build_tracking_to_pipeline_run_track
Create Date: 2026-03-11

Stores final deploy result from Jenkins webhook: {alb_url, build_url, build_result, completed_at}
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '113_add_deploy_result_to_pipeline_run_track'
down_revision: Union[str, None] = '112_add_build_tracking_to_pipeline_run_track'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'pipeline_run_track',
        sa.Column('deploy_result', JSONB(), nullable=True,
                  comment='Final deploy result: {alb_url, build_url, build_result, completed_at}')
    )


def downgrade() -> None:
    op.drop_column('pipeline_run_track', 'deploy_result')
