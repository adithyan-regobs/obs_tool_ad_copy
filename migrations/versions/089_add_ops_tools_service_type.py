"""Add OPS_TOOLS to servicetypeenum

Revision ID: 089_add_ops_tools_service_type
Revises: 088_create_cicd_template_ref_table
Create Date: 2025-12-30

Adds OPS_TOOLS value to the servicetypeenum PostgreSQL enum type.
OPS_TOOLS is for internal operational services that don't require Datadog or Kong gateway.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '089_add_ops_tools_service_type'
down_revision: Union[str, None] = '088_create_cicd_template_ref_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add OPS_TOOLS to the servicetypeenum enum type
    op.execute("ALTER TYPE servicetypeenum ADD VALUE IF NOT EXISTS 'OPS_TOOLS'")


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values directly
    # Would need to recreate the type and update all references
    # For safety, we leave this as a no-op
    pass
