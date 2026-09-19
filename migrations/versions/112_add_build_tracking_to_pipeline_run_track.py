"""Add build tracking columns to pipeline_run_track

Revision ID: 112_add_build_tracking_to_pipeline_run_track
Revises: 111_add_devlift_k8s_vendor_and_postgres_infra
Create Date: 2026-03-11

Adds transaction_queue_code (JSONB), build_stages (JSONB), and build_number (INTEGER)
to pipeline_run_track for per-build queue tracking and stage data.

Also adds 'checkout' and 'verification' to transaction_queue_status_enum.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '112_add_build_tracking_to_pipeline_run_track'
down_revision: Union[str, None] = '111_add_devlift_k8s_vendor_and_postgres_infra'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new columns to pipeline_run_track
    op.add_column(
        'pipeline_run_track',
        sa.Column('build_number', sa.Integer(), nullable=True,
                  comment='Jenkins build number for webhook lookup')
    )
    op.add_column(
        'pipeline_run_track',
        sa.Column('transaction_queue_code', JSONB(), nullable=True,
                  comment='List of transaction_queue codes linked to this build run')
    )
    op.add_column(
        'pipeline_run_track',
        sa.Column('build_stages', JSONB(), nullable=True,
                  comment='Stage breakdown: [{name, status, duration_secs, started_at}]')
    )

    # Index for webhook lookup: pipeline_mst_code + build_number
    op.create_index(
        'idx_pipeline_run_track_pipeline_build',
        'pipeline_run_track',
        ['pipeline_mst_code', 'build_number'],
    )

    # Add checkout and verification to transaction_queue_status_enum
    op.execute(
        "ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS 'checkout'"
    )
    op.execute(
        "ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS 'verification'"
    )


def downgrade() -> None:
    op.drop_index('idx_pipeline_run_track_pipeline_build', table_name='pipeline_run_track')
    op.drop_column('pipeline_run_track', 'build_stages')
    op.drop_column('pipeline_run_track', 'transaction_queue_code')
    op.drop_column('pipeline_run_track', 'build_number')
    # PostgreSQL doesn't support removing enum values directly.
