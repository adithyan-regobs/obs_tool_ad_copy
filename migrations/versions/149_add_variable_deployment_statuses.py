"""Add variable (secrets/configs) deployment statuses to resource_deployment_status_enum

Revision ID: 149_add_variable_deployment_statuses
Revises: 148_add_resource_connection_mst
Create Date: 2026-07-14

Adds the multi-deployment orchestrator's variable-stage milestones to the
resource_deployment_status_enum PostgreSQL enum type, in actual execution
order (secrets → Secrets Manager first, configs → SSM second):
  starting_secrets_deployment / secrets_deployed / secrets_deployment_failed
  starting_config_deployment  / configs_deployed / config_deployment_failed
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "149_add_variable_deployment_statuses"
down_revision: Union[str, None] = "148_add_resource_connection_mst"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_VALUES = [
    "starting_secrets_deployment",
    "secrets_deployed",
    "secrets_deployment_failed",
    "starting_config_deployment",
    "configs_deployed",
    "config_deployment_failed",
]


def upgrade() -> None:
    for value in _NEW_VALUES:
        op.execute(
            f"ALTER TYPE resource_deployment_status_enum ADD VALUE IF NOT EXISTS '{value}'"
        )


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values directly.
    # For safety, we leave this as a no-op.
    pass
