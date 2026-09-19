"""Add devlift_k8s to infra_vendor_enum

Revision ID: 111_add_devlift_k8s_vendor_and_postgres_infra
Revises: 110_add_pipeline_statuses_to_transaction_queue
Create Date: 2026-03-11

Adds 'devlift_k8s' value to the infra_vendor_enum PostgreSQL enum type
for Kubernetes-based infrastructure managed by DevLift.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '111_add_devlift_k8s_vendor_and_postgres_infra'
down_revision: Union[str, None] = '110_add_pipeline_statuses_to_transaction_queue'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE infra_vendor_enum ADD VALUE IF NOT EXISTS 'devlift_k8s'")


def downgrade() -> None:
    # PostgreSQL does not support removing values from enum types.
    # To downgrade, the enum would need to be recreated without 'devlift_k8s'.
    pass
