"""Add ERROR value to resource_deployment_status_enum

Revision ID: 139_add_error_to_resource_deployment_status
Revises: 138_add_resource_deployment_status
Create Date: 2026-06-02

Changes:
  - Add 'error' to resource_deployment_status_enum for manual-commit-detected termination
"""

from alembic import op

revision = "139_add_error_to_resource_deployment_status"
down_revision = "138_add_resource_deployment_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS 'error'")


def downgrade() -> None:
    # Postgres does not support removing enum values; downgrade is a no-op
    pass