"""
Approval Schemas

Request and response shapes for the change-approval API.

Used ONLY by the new /approvals surface. The existing transaction-queue
endpoints and their schemas are untouched.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.service_config_schemas import (
    build_path_for_language,
    is_eks_infra,
    validate_service_path,
)
from app.utils.language_helpers import is_go_language


# ── what the caller may do with one request ─────────────────────────────────
class YouFlags(BaseModel):
    """Resolved per record, per caller — by OpenFGA, never by a column.

    The UI renders its buttons from these and holds no permission logic of its
    own, so a change to the authorization model never needs a frontend change.
    """

    mine: bool = Field(
        ...,
        description=(
            "You submitted this request. Only a warning aid — whether you may "
            "approve your own is the resource group's allow_self_approval rule."
        ),
    )
    can_write_settings: bool = Field(
        default=False,
        description=(
            "You may edit this service — save a change against it, and submit "
            "one. Asked BEFORE anything is created, so a caller who cannot "
            "edit is refused without a ticket or a queue row in their name. "
            "Named for the relation it now reads: the model has no blanket "
            "can_update, and settings and gateway are separately grantable."
        ),
    )
    can_write_settings_reason: Optional[str] = Field(
        None,
        description=(
            "Why can_write_settings is false, in the exact words the write "
            "endpoints raise. Sent with the answer so a caller checking ahead "
            "of time shows the same sentence rather than composing its own."
        ),
    )
    can_write_gateway: bool = Field(
        default=False, description="You may edit this service's Kong routes."
    )
    can_write_secret: bool = Field(
        default=False, description="You may edit this service's secrets."
    )
    can_write_config: bool = Field(
        default=False, description="You may edit this service's config variables."
    )
    # Sent by /approvals/permissions (a service with no request yet) so a
    # caller can tell "view only" from "no access" on Configuration;
    # per-request answers leave it at its default.
    can_view_settings: bool = Field(default=False, description="You may view this service's settings.")
    can_approve: bool = Field(..., description="You may approve, reject or send it back")
    deploy_in_flight: bool = Field(
        default=False,
        description=(
            "A deploy has passed the gate for this change and is running. Not a "
            "permission — a state. The row stays 'approved' throughout (every "
            "deploy path matches that status exactly), so this is the only way "
            "the UI can tell that Revoke would now be refused. Defaults false, "
            "so callers resolved without a row are unaffected."
        ),
    )
    can_deploy: bool = Field(
        default=False,
        description=(
            "You may deploy it once approved. Separate from can_approve on "
            "purpose — an admin deploys but cannot approve, an approver "
            "approves but cannot deploy."
        ),
    )


class ApprovalItem(BaseModel):
    """One change request."""

    id: int = Field(
        ...,
        description=(
            "transaction_queue.id. Every action here takes `code`; this is "
            "exposed because the existing deploy path identifies rows by id."
        ),
    )
    code: str = Field(..., description="Queue code — the id every action takes")
    status: str = Field(..., description="draft | submit | approved | rejected | ...")
    resource_type: Optional[str] = Field(None, description="SERVICE_CONFIG, ...")
    case_ref_code: Optional[str] = Field(
        None,
        description=(
            "Which KIND of change this row is — update_service, add_route, "
            "update_variables. resource_type alone cannot say: a variables row "
            "is SERVICE_CONFIG too."
        ),
    )
    resource_code: Optional[str] = Field(None, description="e.g. the service_config code")
    display_name: Optional[str] = None

    requested_by: str
    requested_by_name: Optional[str] = Field(
        None,
        description=(
            "Display name for requested_by. Resolved server-side because the "
            "row records a user_code and a UUID tells a reader nothing."
        ),
    )
    requested_at: Optional[datetime] = None

    changes: Dict[str, Any] = Field(
        default_factory=dict,
        description="Frozen from/to diff for display: {field: {from, to}}",
    )

    config_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The FULL proposed configuration, so a reviewer sees exactly what "
            "will be written rather than a diff in isolation. `changes` says "
            "which of these fields the request moves."
        ),
    )

    decided_by: Optional[str] = Field(
        None, description="Approver's user_code, or 'rule' when auto-approved"
    )
    decided_by_name: Optional[str] = Field(
        None, description="Display name for decided_by."
    )
    decided_at: Optional[datetime] = None
    decision_comment: Optional[str] = None

    history: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Append-only log. Events: created | submitted | auto-approved | "
            "approved | changes-requested | rejected | approval-revoked | "
            "withdrawn | discarded | seal-broken. Each entry is "
            "{at, by, event, comment?} plus `by_name`, added on the way out so "
            "the UI never has to resolve a user_code itself."
        ),
    )

    you: YouFlags


class ApprovalListResponse(BaseModel):
    approvals: List[ApprovalItem]
    total: int


# ── action bodies ───────────────────────────────────────────────────────────
class DecisionIn(BaseModel):
    """Approve and revoke — the comment is optional."""

    comment: str = Field(default="", max_length=2000)


class RequiredCommentIn(BaseModel):
    """Reject and request-changes. The submitter has to be told why, so a
    blank comment is refused here rather than deeper in the service."""

    comment: str = Field(..., min_length=1, max_length=2000)


class SettingsDraftSaveIn(BaseModel):
    """The settings half alone — POST /transaction/service-settings/{code}.

    Split out of DraftSaveIn so each half of a Save is its own request; the
    service_config code moves to the PATH, everything else keeps DraftSaveIn's
    meaning. The two rows still land in one change set and submit/approve as a
    unit — the split changes who carries the payload, not how it is decided.
    """

    config_snapshot: Dict[str, Any] = Field(
        ...,
        description=(
            "The FULL proposed configuration. Deploy renders this, not a diff, "
            "so a partial snapshot produces a broken deployment."
        ),
    )
    case_ref_code: Optional[str] = Field(
        None, description="Case reference, e.g. update_service"
    )
    queue_code: Optional[str] = Field(
        None,
        description=(
            "Existing queue row to update rather than stack a new one. The "
            "canvas passes the row it last created."
        ),
    )
    ticket_code: Optional[str] = Field(None, description="Ticket this change belongs to")

    @field_validator("config_snapshot")
    @classmethod
    def check_service_path(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """The snapshot is a free-form dict, so service_path arrives here
        unchecked — MainConfigSchema is never applied on this route."""
        raw = v.get("service_path")
        if isinstance(raw, str) and raw.strip():
            # The trailing-/* rule is EKS-only — an ALB path pattern ends that
            # way routinely, and the ECS form appends the wildcard itself. The
            # snapshot names its own infrastructure type, so read it from there.
            v = {**v, "service_path": validate_service_path(
                raw,
                reject_slash_wildcard=is_eks_infra(v.get("infrastructuretype_ref_code")),
            )}
        return v

    @field_validator("config_snapshot")
    @classmethod
    def canonical_build_path(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Same build_path rule as the create/update routes: Go stores `./cmd`,
        every other language `cmd`. The canvas sends the language name at the
        snapshot root; the ref code (go_1_24_language_ref) is the fallback.
        Unknown language: leave the value alone rather than guess."""
        nested = v.get("config")
        block = nested if isinstance(nested, dict) else v
        bp = block.get("build_path")
        if not isinstance(bp, str) or not bp.strip():
            return v
        name = v.get("language_name")
        code = block.get("language_ref_code") or v.get("language_ref_code")
        if isinstance(name, str) and name.strip():
            is_go = is_go_language(name)
        elif isinstance(code, str) and code.strip():
            is_go = code.lower().startswith("go")
        else:
            return v
        fixed = {**block, "build_path": build_path_for_language(bp, is_go)}
        return {**v, "config": fixed} if isinstance(nested, dict) else fixed


class GatewayDraftSaveIn(BaseModel):
    """The gateway half alone — POST /transaction/kong-gateway/{code}.

    An EMPTY list is a valid save: it means every route edit was undone, and
    the pending Kong row is dropped rather than stored empty.
    """

    gateway_groups: List[Dict[str, Any]] = Field(
        ...,
        description=(
            "One entry per route group: the change record (paths added/edited/"
            "deleted, plugins_before/after, regex_priority_before/after) plus "
            "the desired path list. Scope is DERIVED from the path's "
            "service_config code, never sent."
        ),
    )


class ActionResponse(BaseModel):
    ok: bool = True
    # Optional for exactly one case: a gateway save whose empty list dropped
    # the pending row — the save succeeded and there is no record to return.
    approval: Optional[ApprovalItem] = None
    detail: Optional[str] = None


class DeployResponse(BaseModel):
    """Deploy is asynchronous — this says it STARTED, not that it finished."""

    ok: bool = True
    approval: ApprovalItem
    deploy: Dict[str, Any] = Field(
        default_factory=dict,
        description="Passed through from the existing deploy path (workflow_id, status, ...)",
    )
    detail: Optional[str] = None


# ── level 1 of the UI: services with something to review ────────────────────
class ApprovalServiceSummary(BaseModel):
    """One row of the approvals landing page.

    Grouped by service_config — the thing a change is actually made against —
    which is one service in one environment and region. Names come from the
    queue row's own snapshot, so no extra joins are needed to render this.

    Only submitted and approved requests are counted: a draft is private to its
    author and a rejected one is finished, so neither puts a service on this
    list.
    """

    resource_code: str = Field(
        ..., description="service_config code — pass as resource_code to open it"
    )
    services_mst_code: Optional[str] = None
    service_name: Optional[str] = None
    environment: Optional[str] = None
    geo_loc_mst_code: Optional[str] = None
    display_name: Optional[str] = None

    pending_count: int = Field(
        ..., description="Waiting for a decision (status = submit)"
    )
    approved_count: int = Field(
        default=0, description="Approved and waiting to be deployed"
    )
    total_count: int = Field(
        ..., description="Submitted plus approved — this list carries nothing else"
    )
    latest_requested_at: Optional[str] = None


class ApprovalServiceListResponse(BaseModel):
    services: List[ApprovalServiceSummary]
    total: int
