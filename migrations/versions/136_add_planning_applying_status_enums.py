"""Add granular plan/apply status enums to transaction_queue_status_enum

Revision ID: 136_add_planning_applying_status_enums
Revises: 135_make_infrastructure_mst_applications_code_nullable
Create Date: 2026-05-31

Changes:
  - Add 8 new values to transaction_queue_status_enum for Atlantis plan/apply tracking:
    starting_planning, planning, planned_successfully, plan_failed,
    starting_applying, applying, applied_successfully, apply_failed
"""

from alembic import op

revision = "136_add_planning_applying_status_enums"
down_revision = "135_make_infrastructure_mst_applications_code_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    new_values = [
        'starting_planning',
        'planning',
        'planned_successfully',
        'plan_failed',
        'starting_applying',
        'applying',
        'applied_successfully',
        'apply_failed',
    ]
    for value in new_values:
        op.execute(f"ALTER TYPE transaction_queue_status_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres does not support removing enum values — downgrade is a no-op.
    pass