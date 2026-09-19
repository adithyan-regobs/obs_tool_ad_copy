"""Add approval-flow columns to transaction_queue

Revision ID: 156_add_approval_columns_to_transaction_queue
Revises: 155_add_is_write_only_to_variable_mst
Create Date: 2026-08-06

Turns transaction_queue into the change-approval record. Today a config edit
writes straight to service_configs; with a review step that would put an
unapproved change live before anyone looked at it. A change instead becomes a
queue row holding the complete proposed config, and only the deploy step writes
to service_configs.

This is a PREFIX to the pipeline that already exists, not a new one: the table's
default status is already `approved`, with `approved -> pr_raised -> pr_merged`
after it. draft -> submit -> approved slots in front.

No enum change. draft / submit / approved / rejected already exist in
transaction_queue_status_enum, and `request-changes` returns a request to DRAFT
rather than introducing a state of its own — it re-enters through the ordinary
submit edge. The inbox tells a bounced request apart from a never-submitted one
by the last `history` event, not by status.

No backfill. Every existing row is already at `approved` or beyond, i.e. past
the new prefix, and approval only engages for resource groups that opt in via
approval_rule_mst (156).

The lock lane is (transaction_code, table_name): one live (submit/approved)
request per resource per record type. Distinct kinds of change are therefore
serialized against each other only when they share a queue row — which holds
today, since SERVICE_CONFIG and SERVICE_CONFIG_DOCKERFILE are already separate
table_name values. If two independently-approvable kinds of change ever land on
the SAME row, they will need a discriminator column added to this key.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "156_add_approval_columns_to_transaction_queue"
down_revision: Union[str, None] = "155_add_is_write_only_to_variable_mst"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE transaction_queue
            -- frozen {field: {from, to}} diff. Audit rendering only; the apply
            -- step writes config_snapshot wholesale and never merges this.
            ADD COLUMN IF NOT EXISTS changes          JSONB,
            -- Tamper seal over the APPROVED content: sha256 of
            -- config_snapshot, written at the moment of approval and verified
            -- again at deploy. Anyone editing the row in between — including an
            -- in-house developer with direct DB access — breaks the hash and
            -- the deploy refuses. The approval was for content X; this proves
            -- deploy applies exactly X.
            -- NULL until approved, by design: there is nothing sealed yet.
            ADD COLUMN IF NOT EXISTS approved_snapshot_hash VARCHAR(64),
            -- latest decision only; cleared when a request is submitted again.
            -- `history` is the permanent record.
            ADD COLUMN IF NOT EXISTS decided_by       VARCHAR(100),
            ADD COLUMN IF NOT EXISTS decided_at       TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS decision_comment VARCHAR(2000),
            -- append-only event log
            ADD COLUMN IF NOT EXISTS history          JSONB NOT NULL
                                                      DEFAULT '[]'::jsonb
        """
    )

    # Runs on EVERY submit (the lane-lock query) and drives the "other open
    # requests on this resource" panel.
    #
    # IS NOT TRUE, not = false: BaseModel declares is_deleted with a Python-side
    # default only, so rows written outside the ORM can hold NULL. `= false`
    # would silently drop those from the index and from the lock query with it.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_txq_resource_lane
            ON transaction_queue (transaction_code, table_name, status)
            WHERE is_deleted IS NOT TRUE
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_txq_resource_lane")
    op.execute(
        """
        ALTER TABLE transaction_queue
            DROP COLUMN IF EXISTS changes,
            DROP COLUMN IF EXISTS approved_snapshot_hash,
            DROP COLUMN IF EXISTS decided_by,
            DROP COLUMN IF EXISTS decided_at,
            DROP COLUMN IF EXISTS decision_comment,
            DROP COLUMN IF EXISTS history
        """
    )
