"""Add plan verification statuses to resource_deployment_status_enum

Revision ID: 145_add_plan_verification_statuses_to_resource_deployment_status
Revises: 144_add_vendor_deployment_id_and_temporal_agent
Create Date: 2026-06-17

Changes:
  - Add 'starting_plan_verification' to resource_deployment_status_enum
  - Add 'plan_verified_successfully' to resource_deployment_status_enum
  - Add 'destructive_plan_detected' to resource_deployment_status_enum

These back the AI plan-verification step that runs after a successful plan and
before approval in the DeploymentWorkflow.
"""

from alembic import op

revision = "145_add_plan_verification_statuses_to_resource_deployment_status"
down_revision = "144_add_vendor_deployment_id_and_temporal_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS 'starting_plan_verification'")
    op.execute("ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS 'plan_verified_successfully'")
    op.execute("ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS 'destructive_plan_detected'")


def downgrade() -> None:
    # Postgres does not support removing enum values; downgrade is a no-op
    pass
