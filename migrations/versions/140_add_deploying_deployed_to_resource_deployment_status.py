"""Add DEPLOYING and DEPLOYED values to resource_deployment_status_enum

Revision ID: 140_add_deploying_deployed_to_resource_deployment_status
Revises: 139_add_error_to_resource_deployment_status
Create Date: 2026-06-08

Changes:
  - Add 'deploying' to resource_deployment_status_enum (set when PR creation starts)
  - Add 'deployed' to resource_deployment_status_enum (set when PR is merged successfully)
"""

from alembic import op

revision = "140_add_deploying_deployed_to_resource_deployment_status"
down_revision = "139_add_error_to_resource_deployment_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS 'deploying'")
    op.execute("ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS 'deployed'")


def downgrade() -> None:
    # Postgres does not support removing enum values; downgrade is a no-op
    pass
