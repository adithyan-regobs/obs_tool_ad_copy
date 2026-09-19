"""Add PR creation statuses to resource_deployment_status_enum

Revision ID: 142_add_pr_creation_statuses_to_resource_deployment_status
Revises: 141_add_iac_locked_at_to_infra_and_service_config
Create Date: 2026-06-11

Changes:
  - Add 'starting_pr_creation' to resource_deployment_status_enum
  - Add 'creating_pr' to resource_deployment_status_enum
  - Add 'pr_created_successfully' to resource_deployment_status_enum
  - Add 'pr_creation_failed' to resource_deployment_status_enum
"""

from alembic import op

revision = "142_add_pr_creation_statuses_to_resource_deployment_status"
down_revision = "141_add_iac_locked_at_to_infra_and_service_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS 'starting_pr_creation'")
    op.execute("ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS 'creating_pr'")
    op.execute("ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS 'pr_created_successfully'")
    op.execute("ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS 'pr_creation_failed'")


def downgrade() -> None:
    # Postgres does not support removing enum values; downgrade is a no-op
    pass
