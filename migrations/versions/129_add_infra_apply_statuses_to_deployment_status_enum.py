"""Add VERIFYING_PERMISSIONS and PERMISSIONS_MISSING to deployment_status_enum

Revision ID: 129_add_infra_apply_statuses_to_deployment_status_enum
Revises: 128_add_local_to_secret_provider_enum
Create Date: 2026-04-21

Adds two values consumed by the infra-apply Jenkins pipeline flow:

* ``VERIFYING_PERMISSIONS`` — the per-resource pipeline reached the IAM
  verification stage (polls ``aws iam simulate-principal-policy``).
* ``PERMISSIONS_MISSING`` — distinct terminal state from ``FAILED`` so the UI
  can tell the user the resource was created but the tenant role does not yet
  grant the expected actions (typically because the IAM apply pipeline failed).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "129_add_infra_apply_statuses_to_deployment_status_enum"
down_revision: Union[str, None] = "128_add_local_to_secret_provider_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE deployment_status_enum ADD VALUE IF NOT EXISTS 'VERIFYING_PERMISSIONS';")
    op.execute("ALTER TYPE deployment_status_enum ADD VALUE IF NOT EXISTS 'PERMISSIONS_MISSING';")


def downgrade() -> None:
    # Postgres does not support removing enum values directly.
    # To roll back, recreate the type without these values and migrate existing rows.
    pass
