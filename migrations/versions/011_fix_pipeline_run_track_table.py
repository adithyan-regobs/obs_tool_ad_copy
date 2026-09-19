"""Fix pipeline_run_track table schema

Revision ID: 011_fix_pipeline_run_track
Revises: 010_default_pipeline_vendor
Create Date: 2025-11-05 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '011_fix_pipeline_run_track'
down_revision: Union[str, None] = '010_default_pipeline_vendor'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Drop the old pipeline_run_track_model table and create the correct
    pipeline_run_track table matching the model
    """

    # Drop the old incorrectly named table
    op.execute('DROP TABLE IF EXISTS pipeline_run_track_model CASCADE')

    # Create the correct pipeline_run_track table matching the model
    op.create_table(
        'pipeline_run_track',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('pipeline_mst_code', sa.String(length=100), nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('status',
                  postgresql.ENUM('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT',
                                  name='pipeline_run_status_enum', create_type=True),
                  nullable=False,
                  server_default='PENDING'),
        sa.Column('log_url', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=True),
        sa.ForeignKeyConstraint(['pipeline_mst_code'], ['pipeline_mst.code'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Create index on pipeline_mst_code for faster queries
    op.create_index(
        'ix_pipeline_run_track_pipeline_mst_code',
        'pipeline_run_track',
        ['pipeline_mst_code']
    )


def downgrade() -> None:
    """Revert to the old schema"""

    # Drop the correct table
    op.drop_index('ix_pipeline_run_track_pipeline_mst_code', 'pipeline_run_track')
    op.drop_table('pipeline_run_track')

    # Recreate the old incorrectly named table (for rollback compatibility)
    op.create_table(
        'pipeline_run_track_model',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('pipeline_mst_code', sa.String(length=100), nullable=False),
        sa.Column('run_number', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('triggered_by', sa.String(length=255), nullable=True),
        sa.Column('commit_sha', sa.String(length=100), nullable=True),
        sa.Column('commit_message', sa.Text(), nullable=True),
        sa.Column('run_url', sa.String(length=500), nullable=True),
        sa.Column('logs', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['pipeline_mst_code'], ['pipeline_mst.code'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )
