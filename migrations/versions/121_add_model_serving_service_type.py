"""Add MODEL_SERVING to servicetypeenum

Revision ID: 121_add_model_serving_service_type
Revises: 120_add_metadata_json_to_db_permission_mst
Create Date: 2026-04-03

Adds MODEL_SERVING value to the servicetypeenum PostgreSQL enum type
for vLLM model hosting on EKS.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '121_add_model_serving_service_type'
down_revision: Union[str, None] = '120_add_metadata_json_to_db_permission_mst'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE servicetypeenum ADD VALUE IF NOT EXISTS 'MODEL_SERVING'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values directly.
    # A full enum recreation would be needed, which is not safe to automate.
    pass
