"""Add vendor_deployment_id to pipeline_run_track and temporal to pipeline_agent_enum

Revision ID: 144_add_vendor_deployment_id_and_temporal_agent
Revises: 143_add_starting_deployment_to_transaction_queue_status
Create Date: 2026-06-11

Changes:
  - Add 'temporal' to pipeline_agent_enum
  - Add vendor_deployment_id (VARCHAR 255, nullable) to pipeline_run_track
    Generic identifier for the vendor's deployment run:
      GitHub Actions -> GitHub Actions run ID (was github_run_id)
      Jenkins        -> Jenkins build number (was build_number)
      Temporal       -> Temporal workflow_id
"""

from alembic import op
import sqlalchemy as sa

revision = "144_add_vendor_deployment_id_and_temporal_agent"
down_revision = "143_add_starting_deployment_to_transaction_queue_status"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TYPE pipeline_agent_enum ADD VALUE IF NOT EXISTS 'temporal'")

    op.add_column(
        "pipeline_run_track",
        sa.Column("vendor_deployment_id", sa.String(255), nullable=True),
    )


def downgrade():
    op.drop_column("pipeline_run_track", "vendor_deployment_id")
