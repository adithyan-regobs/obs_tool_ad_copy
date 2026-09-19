"""Add deletion-lifecycle values to resource_status_enum

Revision ID: 133_add_deletion_statuses_to_resource_status_enum
Revises: 132_add_awaiting_registration_to_resource_status_enum
Create Date: 2026-04-30

Adds the four deletion-lifecycle values to ``resource_status_enum`` so that
``service_configs.status`` and ``infrastructure_mst.status`` can express:

  SOFT_DELETING / SOFT_DELETED
      DB-only cleanup (variable_mst.is_deleted = true, references cascaded).
      Triggered by the delete_bucket / delete_queue / delete_dynamodb_table / etc.
      post-action components.

  HARD_DELETING / HARD_DELETED
      Actual infra teardown (terragrunt destroy / kubectl delete / etc.).
      Reserved for future use.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "133_add_deletion_statuses_to_resource_status_enum"
down_revision: Union[str, None] = "132_add_awaiting_registration_to_resource_status_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE resource_status_enum ADD VALUE IF NOT EXISTS 'SOFT_DELETING';")
    op.execute("ALTER TYPE resource_status_enum ADD VALUE IF NOT EXISTS 'SOFT_DELETED';")
    op.execute("ALTER TYPE resource_status_enum ADD VALUE IF NOT EXISTS 'HARD_DELETING';")
    op.execute("ALTER TYPE resource_status_enum ADD VALUE IF NOT EXISTS 'HARD_DELETED';")


def downgrade() -> None:
    # Postgres does not support removing enum values directly.
    # To roll back, recreate the type without these values and migrate existing rows.
    pass
