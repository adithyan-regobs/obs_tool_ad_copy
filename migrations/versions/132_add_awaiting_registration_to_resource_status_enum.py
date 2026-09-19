"""Add AWAITING_REGISTRATION to resource_status_enum

Revision ID: 132_add_awaiting_registration_to_resource_status_enum
Revises: 131_add_parent_code_to_infrastructure_mst
Create Date: 2026-04-27

Adds the ``AWAITING_REGISTRATION`` label to ``resource_status_enum`` so on-prem
VM rows can sit in this state after the SSM activation Jenkins job finishes
but before the on-prem SSM agent registers. Other resource types simply never
set this value.

Postgres can't drop a single enum label, so the downgrade is a no-op.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "132_add_awaiting_registration_to_resource_status_enum"
down_revision: Union[str, None] = "131_add_parent_code_to_infrastructure_mst"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE resource_status_enum ADD VALUE IF NOT EXISTS 'AWAITING_REGISTRATION'")


def downgrade() -> None:
    # Postgres does not support removing a single value from an enum without
    # rebuilding the type. Leaving the label in place is harmless.
    pass

