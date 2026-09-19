"""Add starting_deployment to transaction_queue_status_enum

Revision ID: 143_add_starting_deployment_to_transaction_queue_status
Revises: 142_add_pr_creation_statuses_to_resource_deployment_status
Create Date: 2026-06-11

Changes:
  - Add 'starting_deployment' to transaction_queue_status_enum
"""

from alembic import op

revision = "143_add_starting_deployment_to_transaction_queue_status"
down_revision = "142_add_pr_creation_statuses_to_resource_deployment_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS 'starting_deployment'")


def downgrade() -> None:
    # Postgres does not support removing enum values — downgrade is a no-op.
    pass
