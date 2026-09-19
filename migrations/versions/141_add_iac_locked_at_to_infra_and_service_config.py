"""Add iac_locked_at to infrastructure_mst and service_config

Revision ID: 141_add_iac_locked_at_to_infra_and_service_config
Revises: 140_add_deploying_deployed_to_resource_deployment_status
Create Date: 2026-06-10

Changes:
  - Add iac_locked_at (TIMESTAMPTZ, nullable) to infrastructure_mst
  - Add iac_locked_at (TIMESTAMPTZ, nullable) to service_config
  Set by Temporal when an IaC state lock (Atlantis/OpenTofu) blocks deployment.
  Cleared when the lock resolves or the 24h timeout expires.
"""

from alembic import op
import sqlalchemy as sa

revision = "141_add_iac_locked_at_to_infra_and_service_config"
down_revision = "140_add_deploying_deployed_to_resource_deployment_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "infrastructure_mst",
        sa.Column("iac_locked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "service_configs",
        sa.Column("iac_locked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("infrastructure_mst", "iac_locked_at")
    op.drop_column("service_configs", "iac_locked_at")
