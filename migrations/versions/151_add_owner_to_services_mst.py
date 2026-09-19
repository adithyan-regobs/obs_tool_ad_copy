"""Add owner_user_code to services_mst

Revision ID: 151_add_owner_to_services_mst
Revises: 150_add_deploying_secrets_configs_statuses
Create Date: 2026-07-16

Adds a nullable owner reference (owner_user_code -> user_mst.code) to services_mst
so the service Info tab can show who owns a service.

Existing services are left NULL (surfaced as "No owner assigned" in the UI) and
new services get their owner set on creation.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "151_add_owner_to_services_mst"
down_revision: Union[str, None] = "150_add_deploying_secrets_configs_statuses"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE services_mst
        ADD COLUMN IF NOT EXISTS owner_user_code VARCHAR(100)
        """
    )
    op.execute(
        """
        ALTER TABLE services_mst
        ADD CONSTRAINT fk_services_mst_owner_user_code
        FOREIGN KEY (owner_user_code)
        REFERENCES user_mst (code)
        ON DELETE SET NULL
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE services_mst DROP CONSTRAINT IF EXISTS fk_services_mst_owner_user_code"
    )
    op.execute("ALTER TABLE services_mst DROP COLUMN IF EXISTS owner_user_code")
