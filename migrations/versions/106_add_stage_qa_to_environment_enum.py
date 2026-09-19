"""Add stage and qa to environment_enum

Revision ID: 106_add_stage_qa_to_environment_enum
Revises: 105_add_stage_qa_to_enviornment_enum
Create Date: 2026-02-21

Adds 'stage' and 'qa' values to the correctly-spelled environment_enum
PostgreSQL enum type used by kong_route_configs, service_config,
sidecar_config, chat_info, pipeline_mst, and other tables.

Note: Migration 105 added these values to the typo'd 'enviornment_enum'
(used by infrastructure_mst and infra_vendor_accounts_mst) but missed
the correctly-spelled 'environment_enum'.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '106_add_stage_qa_to_environment_enum'
down_revision: Union[str, None] = '105_add_stage_qa_to_enviornment_enum'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE environment_enum ADD VALUE IF NOT EXISTS 'stage'"
    )
    op.execute(
        "ALTER TYPE environment_enum ADD VALUE IF NOT EXISTS 'qa'"
    )


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values directly.
    pass
