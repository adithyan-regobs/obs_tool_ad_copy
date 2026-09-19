"""Add approval_rule_mst — does a change need review, and who may approve it

Revision ID: 157_add_approval_rule_mst
Revises: 156_add_approval_columns_to_transaction_queue
Create Date: 2026-08-06

Three questions OpenFGA structurally cannot answer, because none of them has a
user in it:

    require_approval      does this change need review at all?
    allow_self_approval   may the submitter approve their own request?
    allow_approver_edit   may the approver change the request while approving?

"May raj approve this?" DOES name a user, so it stays in OpenFGA as can_approve.
This table holds only the policy around that check.

Every default reproduces today's behaviour exactly: require_approval FALSE means
no gate, and a resource group with no row here is untouched by any of this. That
is why there is no backfill — the gate engages per group, when someone opts in.

    require_approval=false   submit goes straight to `approved` with
                             decided_by='rule'. Deploy rights are still checked;
                             auto-approval skips the reviewer, not the executor.

    allow_self_approval      the four-eyes switch. A two-person team needs it
                             true or nothing ships; a regulated prod service
                             needs it false.

    allow_approver_edit      some clients want the approver to fix the request
                             in place rather than bounce it back. OFF by
                             default because it overrides the OpenFGA decision:
                             the edited content is approved without the
                             submitter's can_update ever being re-checked
                             against it.

                             It also weakens allow_self_approval=false — an
                             approver who edits then approves is approving
                             content they partly authored. That is the client's
                             call to make, but the audit must not hide it: the
                             service layer MUST append an `amended-by-approver`
                             history event naming them and re-freeze `changes`
                             against current values, or the record shows the
                             submitter proposing something they never wrote.
                             approved_snapshot_hash needs no special handling —
                             it is sealed at approval, so it already covers the
                             edited content rather than the original.

SCOPE — resource group, resolved by a plain FK join:

    service_configs.services_mst_code
      -> services_mst.resource_group_mst_code
        -> approval_rule_mst (tenant_code, resource_group_mst_code)

    infrastructure_mst.resource_group_mst_code
        -> approval_rule_mst (tenant_code, resource_group_mst_code)

A real FK column rather than a polymorphic (entity_type, entity_id) pair: with
one scope, entity_type would hold the same value on every row and carry no
information, while costing a condition in every query. The FK also makes a rule
pointing at a non-existent group impossible to write, rather than something you
discover when the gate silently never fires.

Two deliberate omissions, both additive later:

  * No `environment` column, so a rule covers dev, stage and prod alike. If
    gating prod while leaving dev free turns out to matter, add a nullable
    environment column and resolve exact-match before NULL.
  * No per-service override. If one sensitive service needs its own policy, add
    a nullable services_mst_code with a check constraint that exactly one scope
    is set, and rank service above resource_group in the resolver.

NOTE for the resolver: a row with is_deleted = TRUE must be treated as ABSENT —
i.e. as the default, no gate. Reading it as "a row exists with require_approval
false" happens to give the same answer today, but the two stop agreeing the
moment a default changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "157_add_approval_rule_mst"
down_revision: Union[str, None] = "156_add_approval_columns_to_transaction_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "approval_rule_mst",
        # BaseModel columns
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),

        # Scope
        sa.Column("tenant_code", sa.String(100), nullable=False),
        sa.Column("resource_group_mst_code", sa.String(100), nullable=False),

        # Policy — every default is today's behaviour
        sa.Column("require_approval", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("allow_self_approval", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("allow_approver_edit", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),

        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_approval_rule_mst_code"),
        sa.ForeignKeyConstraint(["tenant_code"], ["tenants_mst.code"],
                                name="fk_approval_rule_tenant_code",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resource_group_mst_code"], ["resource_group_mst.code"],
                                name="fk_approval_rule_resource_group",
                                ondelete="CASCADE"),
    )

    # One LIVE rule per (tenant, resource group).
    #
    # A partial unique index, not a table constraint: soft-deleted rows must be
    # able to coexist with the row that replaced them, or re-creating a rule for
    # a group that once had one would fail.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_approval_rule_tenant_rg
            ON approval_rule_mst (tenant_code, resource_group_mst_code)
            WHERE is_deleted IS NOT TRUE
        """
    )

    # The resolver reads by exactly this pair, on every create (to pick the
    # entry status) and every approve (to enforce self-approval).
    op.create_index(
        "idx_approval_rule_lookup",
        "approval_rule_mst",
        ["tenant_code", "resource_group_mst_code"],
    )


def downgrade() -> None:
    op.drop_index("idx_approval_rule_lookup", table_name="approval_rule_mst")
    op.execute("DROP INDEX IF EXISTS uq_approval_rule_tenant_rg")
    op.drop_table("approval_rule_mst")
