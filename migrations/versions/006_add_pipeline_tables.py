"""Add pipeline tables

Revision ID: 006_pipeline_tables
Revises: 005_nullable_fields
Create Date: 2025-11-04 17:03:38.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '006_pipeline_tables'
down_revision: Union[str, None] = '005_nullable_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create pipeline-related tables"""

    # Create language_ref table
    op.create_table(
        'language_ref',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Create pipeline_vendor_mst table
    op.create_table(
        'pipeline_vendor_mst',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Create pipeline_mst table
    op.create_table(
        'pipeline_mst',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('service_mst_code', sa.String(length=100), nullable=False),
        sa.Column('pipeline_vendor_mst_code', sa.String(length=100), nullable=False),
        sa.Column('repo_url', sa.String(length=500), nullable=False),
        sa.Column('repo_branch', sa.String(length=100), nullable=False, server_default='main'),
        sa.Column('language_ref_code', sa.String(length=100), nullable=False),
        sa.Column('environment', postgresql.ENUM('dev', 'staging', 'prod', name='environment_enum', create_type=False), nullable=False),
        sa.Column('folder', sa.String(length=500), nullable=True),
        sa.Column('authentication_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['service_mst_code'], ['services_mst.code'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['pipeline_vendor_mst_code'], ['pipeline_vendor_mst.code'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['language_ref_code'], ['language_ref.code'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    # Create pipeline_run_track_model table
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


def downgrade() -> None:
    """Drop pipeline-related tables"""
    op.drop_table('pipeline_run_track_model')
    op.drop_table('pipeline_mst')
    op.drop_table('pipeline_vendor_mst')
    op.drop_table('language_ref')
