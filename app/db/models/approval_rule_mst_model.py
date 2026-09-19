"""
Approval Rule Model

Approval POLICY for a resource group. Three booleans, scoped to one group.

The split against OpenFGA is by whether a user appears in the question:

    "May raj approve this?"          -> OpenFGA (can_approve)  — a relationship
    "Does this need approval?"       -> here                   — configuration
    "May the submitter self-approve?"-> here
    "May the approver edit it?"      -> here

Every default reproduces today's behaviour, so a resource group with NO row here
is untouched by the approval flow. The gate engages per group, when someone
opts in.
"""

from sqlalchemy import Boolean, Column, ForeignKey, String
from sqlalchemy.orm import relationship

from app.db.models.base_model import BaseModel


class ApprovalRuleMstModel(BaseModel):
    """
    Per-resource-group approval policy.

    Attributes:
        require_approval: Does a change here need review before it deploys?
        allow_self_approval: May the submitter approve their own request?
        allow_approver_edit: May the approver change the request while approving?
    """

    __tablename__ = "approval_rule_mst"

    # ========== SCOPE ==========
    tenant_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Tenant for data isolation"
    )

    resource_group_mst_code = Column(
        String(100),
        ForeignKey("resource_group_mst.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Resource group this policy applies to"
    )

    # ========== POLICY ==========
    require_approval = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "False: submit goes straight to approved with decided_by='rule'. "
            "Deploy rights are still checked — auto-approval skips the "
            "reviewer, not the executor."
        )
    )

    allow_self_approval = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment=(
            "The four-eyes switch. False: the submitter cannot approve their "
            "own request."
        )
    )

    allow_approver_edit = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "True: the approver may fix the request in place instead of "
            "bouncing it back. Overrides the OpenFGA decision — the edited "
            "content is approved without the submitter's can_update ever "
            "being re-checked against it, and an approver who edits then "
            "approves is approving content they partly authored. The service "
            "layer MUST append an 'amended-by-approver' history event naming "
            "them and re-freeze `changes`, or the record shows the submitter "
            "proposing something they never wrote. approved_snapshot_hash is "
            "sealed at approval, so it already covers the edited content."
        )
    )

    # ========== RELATIONSHIPS ==========
    tenant = relationship("TenantsMstModel", foreign_keys=[tenant_code])
    resource_group = relationship(
        "ResourceGroupMstModel", foreign_keys=[resource_group_mst_code]
    )

    def __repr__(self) -> str:
        """String representation for debugging."""
        return (
            f"<ApprovalRule(rg='{self.resource_group_mst_code}', "
            f"require={self.require_approval}, "
            f"self={self.allow_self_approval}, "
            f"approver_edit={self.allow_approver_edit})>"
        )
