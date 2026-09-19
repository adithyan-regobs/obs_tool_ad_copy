"""Add transaction_queue.deploy_started_at — the in-flight marker the revoke
guard reads.

Revoke refuses anything that is not APPROVED, and its docstring assumed that
"once a deploy starts, the row leaves that status". It does not: the deploy
GATE (/approvals/{code}/deploy) verifies the seal and writes a deploy-started
history event but deliberately leaves the status alone, and the row only
reaches STARTING_DEPLOYMENT inside a Temporal activity seconds later. For that
whole gap an approver could revoke a change that was already shipping, which
nulls the seal and decided_by/at while the deploy carries on regardless. It has
happened in production (queue-8afff488971b: approved 18:29:22, deploy-started
18:29:25, approval-revoked 18:29:27, deployed 18:33:19 — with no approver and
no seal on the row that shipped).

A separate column rather than a new status: every deploy path checks for
status == APPROVED exactly (validate_deployable_queue_items, the multiple-deploy
route, the failure-retry reset, and the UI's Deploy button), so moving the row
out of APPROVED in the gate would break the deploy for everyone. This marker
sits beside the status and changes nothing that reads it.

Revision ID: 167_add_deploy_started_at
Revises: 166_add_production_deployment_track
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "167_add_deploy_started_at"
down_revision: Union[str, None] = "166_add_production_deployment_track"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transaction_queue",
        sa.Column(
            "deploy_started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment=(
                "Set by the deploy gate; cleared when a deploy fails and the "
                "row returns to approved. Non-null means a deploy is in flight, "
                "which is what blocks a revoke."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("transaction_queue", "deploy_started_at")
