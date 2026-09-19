"""Add deploying_secrets / deploying_configs to resource_deployment_status_enum

Revision ID: 150_add_deploying_secrets_configs_statuses
Revises: 149_add_variable_deployment_statuses
Create Date: 2026-07-15

In-progress statuses for the variable stage, matching the infra convention
(planning / applying): written while the Secrets Manager / SSM writes are
actually running, replacing starting_secrets_deployment /
starting_config_deployment as the in-flight status.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "150_add_deploying_secrets_configs_statuses"
down_revision: Union[str, None] = "149_add_variable_deployment_statuses"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_VALUES = [
    "deploying_secrets",
    "deploying_configs",
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