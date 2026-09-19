"""Add approval status enums to transaction_queue_status_enum

Revision ID: 137_add_approval_status_enums
Revises: 136_add_planning_applying_status_enums
Create Date: 2026-06-02

Changes:
  - Add 3 new values to transaction_queue_status_enum for GitHub PR approval tracking:
    starting_approval, approved_successfully, approval_failed
"""

from alembic import op

revision = "137_add_approval_status_enums"
down_revision = "136_add_planning_applying_status_enums"
branch_labels = None
depends_on = None


def upgrade() -> None:
    new_values = [
        'starting_approval',
        'approved_successfully',
        'approval_failed',
    ]
    for value in new_values:
        op.execute(f"ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres does not support removing enum values — downgrade is a no-op.
    pass
