"""Add jenkins to pipeline_agent_enum

Revision ID: 109_add_jenkins_to_pipeline_agent_enum
Revises: 108_add_config_jsonb_to_tenants_mst
Create Date: 2026-03-11

Adds 'jenkins' value to the pipeline_agent_enum PostgreSQL enum type.
Required by the signup flow which creates default Jenkins pipeline vendor configs.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '109_add_jenkins_to_pipeline_agent_enum'
down_revision: Union[str, None] = '108_add_config_jsonb_to_tenants_mst'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE pipeline_agent_enum ADD VALUE IF NOT EXISTS 'jenkins'"
    )


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values directly.
    pass
