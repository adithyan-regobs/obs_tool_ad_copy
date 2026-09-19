"""
GitOps Queue Model

Stores items queued for batch deployment via GitOps workflow.
Supports "Add to Queue" → "Deploy All" pattern for creating single PRs
with multiple infrastructure/service config changes.

Status Lifecycle:
    approved → pr_raised → pr_merged/pr_closed
    approved → deleted (removed before deploy)
"""

from sqlalchemy import Column, String, Integer, TIMESTAMP, Enum as SqlEnum, ForeignKey, Index, BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from enum import Enum

from app.db.models.base_model import BaseModel
from app.core.enum import WorkflowSourceTableEnum


class GitopsQueueStatusEnum(str, Enum):
    PENDING = "pending"
    DELETED = "deleted"
    PR_RAISED = "pr_raised"
    PR_MERGED = "pr_merged"
    PR_CLOSED = "pr_closed"


class TransactionQueueStatusEnum(str, Enum):
    """Status of a queue item."""
    DRAFT = "draft"
    SUBMIT = "submit"
    APPROVED = "approved"
    STARTING_DEPLOYMENT = "starting_deployment"
    REJECTED = "rejected"
    COMMIT = "commit"
    PR_RAISED = "pr_raised"
    PR_DRAFT = "pr_draft"
    PR_APPROVED = "pr_approved"
    PR_REJECTED = "pr_rejected"
    DEPLOYING = "deploying"
    PR_MERGED = "pr_merged"
    PROVISIONING = "provisioning"
    BUILDING = "building"
    DEPLOYED = "deployed"
    FAILED = "failed"
    CHECKOUT = "checkout"
    VERIFICATION = "verification"
    # Granular Atlantis plan/apply statuses (for frontend P0 alerting)
    STARTING_PLANNING = "starting_planning"
    PLANNING = "planning"
    PLANNED_SUCCESSFULLY = "planned_successfully"
    PLAN_FAILED = "plan_failed"
    STARTING_APPLYING = "starting_applying"
    APPLYING = "applying"
    APPLIED_SUCCESSFULLY = "applied_successfully"
    APPLY_FAILED = "apply_failed"
    STARTING_APPROVAL = "starting_approval"
    APPROVED_SUCCESSFULLY = "approved_successfully"
    APPROVAL_FAILED = "approval_failed"


# The case_ref code that marks a queue row as a Kong gateway (add-route)
# change. table_name cannot tell a gateway change from a settings one — both
# live on SERVICE_CONFIG — so every "is this a gateway row?" question asks
# this instead, through the two helpers below.
GATEWAY_CASE_REF = "add_route"


def is_gateway_row(item) -> bool:
    """True when a loaded queue row is a Kong gateway (add_route) change.

    New rows carry table_name=SERVICE_CONFIG with case_ref_code="add_route";
    rows written before the flip still say table_name=KONG_ROUTE. Both mean
    the same thing, and every Python-side branch should ask HERE rather than
    hand-rolling the pair — the KONG_ROUTE half disappears once the old rows
    are cleared, and this is the one place that then changes.
    """
    if getattr(item, "table_name", None) == WorkflowSourceTableEnum.KONG_ROUTE:
        return True
    return (
        getattr(item, "table_name", None) == WorkflowSourceTableEnum.SERVICE_CONFIG
        and (getattr(item, "case_ref_code", None) or "") == GATEWAY_CASE_REF
    )


def gateway_rows_clause():
    """SQL twin of is_gateway_row: a filter/join condition matching gateway rows.

    Usable in any query over TransactionQueueModel — WHERE filters and
    outer-join ON clauses alike. Same single-place-to-change property as
    is_gateway_row.
    """
    from sqlalchemy import and_, or_

    return or_(
        and_(
            TransactionQueueModel.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG,
            TransactionQueueModel.case_ref_code == GATEWAY_CASE_REF,
        ),
        TransactionQueueModel.table_name == WorkflowSourceTableEnum.KONG_ROUTE,
    )


class TransactionQueueModel(BaseModel):
    """
    Model for tracking items queued for batch GitOps deployment.

    Each item stores a CONFIG SNAPSHOT at the time of adding to queue.
    This ensures:
    1. Predictable deploys - what you queue is what gets deployed
    2. Accurate previews - HCL generated from snapshot matches final output
    3. Safe PR refresh - regeneration uses same config values, not current DB

    Usage:
        1. User clicks "Add to Queue" → creates record with config_snapshot, status=approved
        2. User can remove item → status=deleted
        3. User clicks "Deploy All" → generates HCL from snapshots, creates PR
        4. Items updated with pr_number, pr_url, status=pr_raised
        5. When PR is merged → status=pr_merged
        6. When PR is closed without merge → status=pr_closed
        7. PR refresh regenerates from snapshots (not current DB values)
    """

    __tablename__ = "transaction_queue"

    # Primary Key
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Queue ID (used as reference in API calls)
    code = Column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
        comment="Unique queue identifier code (QueueID)"
    )

    # User who added this item
    user_code = Column(
        String(100),
        nullable=False,
        index=True,
        comment="User code who added this item to queue"
    )

    # POLYMORPHIC REFERENCE (for reverse lookup to source entity)
    transaction_code = Column(
        String(100),
        nullable=True,
        index=True,
        comment="Code of the entity that created this workflow (e.g., service_config.code)"
    )
    # belong to which source table
    table_name = Column(
        SqlEnum(WorkflowSourceTableEnum, name="workflow_source_table_enum"),
        nullable=True,
        index=True,
        comment="Source table name: SERVICE_CONFIG, ALERT_CONFIG, INFRASTRUCTURE, KONG_ROUTE, PIPELINE, SERVICE_CONFIG_DOCKERFILE"
    )


    # CONFIG SNAPSHOT - Full config data at time of adding to queue
    # This is the source of truth for HCL generation and PR refresh
    config_snapshot = Column(
        JSONB,
        nullable=False,
        comment="Complete config as JSON at time of adding to queue"
    )

    # Display name for UI
    display_name = Column(
        String(255),
        nullable=True,
        comment="Human-readable display name for UI"
    )

    # Case reference (FK to case_ref table)
    case_ref_code = Column(
        String(100),
        ForeignKey("case_ref.code", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Foreign key reference to case_ref table"
    )

    script_access_key = Column(
        JSONB,
        nullable=True,
        comment="S3 object keys for uploaded HCL artifacts as JSON (e.g., {\"original_s3_key\": \"sqs/payment-queue.hcl\", \"preview\": \"preview/sqs/payment-queue.hcl\"})"
    )

    # Ticket reference (FK to ticket table)
    ticket_code = Column(
        String(100),
        ForeignKey("ticket.code", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Foreign key reference to ticket table"
    )

    # Status workflow
    status = Column(
        SqlEnum(
            TransactionQueueStatusEnum,
            name="transaction_queue_status_enum",
            values_callable=lambda enum: [e.value for e in enum]
        ),
        nullable=False,
        default=TransactionQueueStatusEnum.APPROVED,
        comment="Status: approved, deleted, pr_raised, pr_merged, pr_closed"
    )

     # Timestamps
    status_last_updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When item status was last updated"
    )

    # ========== APPROVAL FLOW (migration 155) ==========
    # draft -> submit -> approved -> deployed, sitting IN FRONT of the GitOps
    # statuses that already follow `approved`. Rows in a resource group with no
    # approval_rule_mst entry enter at `approved` exactly as before and never
    # touch these columns.

    # The lock lane is (transaction_code, table_name): one live
    # (submit/approved) request per resource per record type. Two kinds of
    # change are serialized against each other only when they share a queue
    # row — which holds today, since SERVICE_CONFIG and
    # SERVICE_CONFIG_DOCKERFILE are already separate table_name values.

    # Frozen {field: {"from": x, "to": y}} diff, captured at submit.
    # Audit rendering ONLY — the apply step writes config_snapshot wholesale
    # and never merges this back in.
    changes = Column(
        JSONB,
        nullable=True,
        comment="Frozen from/to diff for display: {field: {from, to}}"
    )

    # Tamper seal over the APPROVED content — sha256 of config_snapshot,
    # written at the moment of approval and verified again at deploy.
    #
    #   approve  seal   = sha256(canonical(config_snapshot))
    #   deploy   verify = seal still matches config_snapshot, else refuse
    #
    # This closes the approve -> deploy window: anyone editing the row in
    # between, including an in-house developer going straight at the database,
    # breaks the hash and the deploy stops. The approval was for content X;
    # the seal proves deploy applies exactly X.
    #
    # NULL until approved, by design — there is nothing sealed yet. If the
    # approver edits under allow_approver_edit, the seal is taken AFTER their
    # edit, over what they actually approved.
    #
    # Note this seals the REQUEST, not the resource. It does not detect the
    # service itself drifting during review; that would be a separate baseline
    # recorded at submit.
    approved_snapshot_hash = Column(
        String(64),
        nullable=True,
        comment="sha256 of config_snapshot, sealed at approval; verified at deploy"
    )

    # Latest decision only — cleared when a request is submitted again.
    # `history` is the permanent record; never read these for an audit trail.
    decided_by = Column(
        String(100),
        nullable=True,
        comment="Approver's user_code, or 'rule' when auto-approved"
    )
    decided_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Set by the deploy GATE (/approvals/{code}/deploy), cleared when a deploy
    # fails and the row is returned to APPROVED for retry.
    #
    # This exists because revoke() cannot use the status to tell whether a
    # deploy has begun. The gate deliberately leaves the row APPROVED — every
    # deploy path matches that status exactly — so the row stays revocable for
    # the seconds between the gate passing and the Temporal activity flipping
    # it to STARTING_DEPLOYMENT. A revoke landing in that gap nulls the seal
    # and decided_by/at while the deploy ships anyway, leaving a deployed
    # change with no approver on it.
    #
    # Non-null means "a deploy is in flight": revoke refuses, and the caller's
    # YouFlags carry it as deploy_in_flight so the UI stops drawing the button.
    deploy_started_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Deploy gate passed; blocks revoke until cleared or the row moves on",
    )
    decision_comment = Column(
        String(2000),
        nullable=True,
        comment="Mandatory on reject and request-changes"
    )

    # Append-only event log of {at, by, event, comment?}.
    # JSONB does not observe in-place mutation — ALWAYS reassign:
    #     row.history = row.history + [event]     correct
    #     row.history.append(event)               silently lost
    history = Column(
        JSONB,
        nullable=False,
        server_default="[]",
        comment="Append-only transition log; reassign, never mutate"
    )

    # Tenant tracking
    tenant_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Tenant code"
    )

    # Timestamps
    created_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When item was added to queue"
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Last update time"
    )
    deleted_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Soft delete timestamp (when status changed to deleted)"
    )

    # Relationships
    tenant = relationship("TenantsMstModel", foreign_keys=[tenant_code])
    case_ref = relationship("CaseRefModel", foreign_keys=[case_ref_code], back_populates="transaction_queues")
    ticket = relationship("TicketModel", foreign_keys=[ticket_code])
    workflow_mappings = relationship("TransactionQueueWorkflowMappingModel", back_populates="transaction_queue")

    # Composite indexes and constraints
    __table_args__ = (
        # Get pending items for a user
        Index('idx_transaction_queue_user_status', 'user_code', 'status'),
        # Get items by tenant
        Index('idx_transaction_queue_tenant', 'tenant_code', 'status'),
        # Get items by case reference
        Index('idx_transaction_queue_case_ref_code', 'case_ref_code'),
    )

    def __repr__(self):
        return f"<TransactionQueueModel(id={self.id}, user={self.user_code}, status={self.status})>"

    def soft_delete(self):
        """Soft delete this queue item (remove from queue before deploy)."""
        self.is_deleted = True
        self.is_active = False
        self.deleted_at = func.now()

    def mark_pr_raised(self, pr_number: int, pr_url: str, git_branch: str, commit_sha: str, gitops_workflow_id: int):
        """Mark item as having a PR raised."""
        self.status = GitopsQueueStatusEnum.PR_RAISED
        # Note: PR tracking fields need to be added to model if using this method
        # self.pr_number = pr_number
        # self.pr_url = pr_url
        # self.git_branch = git_branch
        # self.commit_sha = commit_sha
        # self.gitops_workflow_id = gitops_workflow_id

    def mark_pr_merged(self):
        """Mark item's PR as merged."""
        self.status = GitopsQueueStatusEnum.PR_MERGED

    def mark_pr_closed(self):
        """Mark item's PR as closed without merge."""
        self.status = GitopsQueueStatusEnum.PR_CLOSED
