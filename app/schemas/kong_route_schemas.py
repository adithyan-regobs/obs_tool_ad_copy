"""
Kong Route Configuration Schemas

Pydantic schemas for Kong Gateway route configuration requests and responses.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator
from app.core.enum import HttpMethodEnum, DeploymentStatusEnum, EnvironmentEnum, WorkflowSourceTableEnum


class CreateKongRouteRequest(BaseModel):
    """Request schema for creating a Kong Gateway route"""

    api_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="API identifier in kong_configs (e.g., 'user-api', 'payment-service')"
    )
    http_method: HttpMethodEnum = Field(
        ...,
        description="HTTP method for the route"
    )
    route_path: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Kong route pattern (e.g., '~/api/v1/users$', '/api/v1/users$')"
    )
    services_mst_code: Optional[str] = Field(
        None,
        max_length=100,
        description="Optional service code from services_mst table"
    )

    @field_validator("api_name")
    @classmethod
    def validate_api_name_whitespace(cls, v: str) -> str:
        """Validate and strip API name"""
        v = v.strip()
        if not v:
            raise ValueError("API name cannot be empty or whitespace")
        return v

    @field_validator("route_path")
    @classmethod
    def validate_route_path_whitespace(cls, v: str) -> str:
        """Validate and strip route path"""
        v = v.strip()
        if not v:
            raise ValueError("Route path cannot be empty or whitespace")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "api_name": "user_api",
                "http_method": "GET",
                "route_path": "~/api/v1/users$",
                "services_mst_code": "SVC_12345"
            }
        }


class KongRouteResponse(BaseModel):
    """Response schema for Kong route creation"""

    success: bool = Field(..., description="Operation success status")
    route_code: str = Field(..., description="Unique route code (e.g., KRC_ABC123)")
    route_id: int = Field(..., description="Database route ID")
    creation_status: DeploymentStatusEnum = Field(
        ...,
        description="Current deployment status"
    )
    pr_number: Optional[int] = Field(None, description="GitHub PR number")
    pr_url: Optional[str] = Field(None, description="GitHub PR URL")
    feature_branch: Optional[str] = Field(None, description="Git feature branch name")
    message: str = Field(..., description="Operation result message")

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "route_code": "KRC_ABC12345",
                "route_id": 42,
                "creation_status": "PR_CREATED",
                "pr_number": 123,
                "pr_url": "https://github.com/org/repo/pull/123",
                "feature_branch": "gateway/route-user-api-1731974000",
                "message": "Kong route created successfully and PR #123 opened"
            }
        }


class KongRouteListResponse(BaseModel):
    """Response schema for listing Kong routes"""

    routes: list[dict] = Field(..., description="List of Kong route configurations")
    total: int = Field(..., description="Total number of routes")

    class Config:
        json_schema_extra = {
            "example": {
                "routes": [
                    {
                        "id": 1,
                        "code": "KRC_ABC123",
                        "api_name": "user_api",
                        "http_method": "GET",
                        "route_path": "~/api/v1/users$",
                        "creation_status": "PR_CREATED",
                        "created_at": "2025-01-18T10:00:00Z"
                    }
                ],
                "total": 1
            }
        }


# ── Dedicated Kong Route Config API (used by Kong Gateway tab) ──────────────

class KongRouteConfigCreateRequest(BaseModel):
    """
    Request to create or update a Kong Gateway route config.

    Dedicated endpoint — no infrastructuretype_ref_code needed.
    Saves directly to kong_route_configs table.

    Upsert:
    - code provided → update existing record
    - code absent → create new record
    """

    code: Optional[str] = Field(
        None,
        description="Kong route code to UPDATE. Omit to create NEW record"
    )

    service_mst_code: str = Field(
        ...,
        description="Service code that this route belongs to"
    )

    application_code: str = Field(
        ...,
        description="Application code (for tenant validation)"
    )

    environment: EnvironmentEnum = Field(
        ...,
        description="Target environment (dev, stage, qa, prod)"
    )

    geo_loc_mst_code: str = Field(
        ...,
        description="Geographic location code (e.g., region-aspora-mumbai)"
    )

    api_name: Optional[str] = Field(
        None,
        description="API identifier. If not provided, uses the service name"
    )

    http_method: str = Field(
        ...,
        description="HTTP method (GET, POST, PUT, PATCH, DELETE)"
    )

    route_path: str = Field(
        ...,
        description="Plain route path (e.g., /api/v1/users, /files/:id, /files/*). "
                    "Compiled to a Kong regex pattern (~/...$) server-side. A value "
                    "already in Kong form is accepted as-is."
    )

    plugins: list[str] = Field(
        default_factory=list,
        description="Name-only Kong plugins for this route (e.g. ['JWT', 'CORS']). "
                    "Include 'JWT' for secured routes; its presence derives secured/public."
    )

    regex_priority: int = Field(
        default=0,
        description="Kong regex_priority for the route-group. Overrides unsupported — keep 0."
    )

    route_group_key: Optional[str] = Field(
        None,
        max_length=200,
        description="Human-readable terragrunt route-group name (e.g. 'login-routes', "
                    "'payments-open'). Sent from the frontend so the generated kong_configs "
                    "keys are self-documenting. Falls back to the service name if omitted."
    )

    @field_validator("geo_loc_mst_code")
    @classmethod
    def require_geo_loc(cls, v: str) -> str:
        """
        Reject an empty region up front.

        It is a foreign key AND part of a route group's identity, so "" reaches the
        DB as an FK violation — and Save posts one request per path, so a service
        with 100 paths produced 100 identical 500s at once. A service imported from
        terragrunt has no service_config row, so the canvas node carries no
        geoLocCode and the tab used to send blank.
        """
        v = (v or "").strip()
        if not v:
            raise ValueError(
                "geo_loc_mst_code is required — this service has no region configured"
            )
        return v

    @field_validator("route_group_key")
    @classmethod
    def normalize_route_group_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("plugins")
    @classmethod
    def normalize_plugins(cls, v: list[str]) -> list[str]:
        """Trim, drop blanks, and de-duplicate plugin names (order preserved)."""
        seen: set[str] = set()
        cleaned: list[str] = []
        for name in v:
            name = (name or "").strip()
            if name and name not in seen:
                seen.add(name)
                cleaned.append(name)
        return cleaned

    @field_validator("http_method")
    @classmethod
    def validate_http_method(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            raise ValueError(f"Invalid HTTP method: {v}")
        return v

    @field_validator("route_path")
    @classmethod
    def validate_route_path(cls, v: str) -> str:
        # Accept a plain path (e.g. /api/v1/users, /files/:id) — the service
        # compiles it to a Kong regex (~/...$) before saving. A pre-compiled
        # Kong pattern is also accepted (the compiler is idempotent).
        v = v.strip()
        if not v:
            raise ValueError("Route path cannot be empty")
        return v

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "service_mst_code": "f7513b9e-c6ab-4a8e-a61b-b1afeb604829",
                    "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
                    "environment": "dev",
                    "geo_loc_mst_code": "region-aspora-mumbai",
                    "api_name": "user-api",
                    "http_method": "GET",
                    "route_path": "~/api/v1/users$"
                }
            ]
        }


class KongRouteConfigCreateResponse(BaseModel):
    """Response after creating/updating a Kong route config."""

    table_name: WorkflowSourceTableEnum = Field(
        default=WorkflowSourceTableEnum.KONG_ROUTE,
        description="Always KONG_ROUTE"
    )
    code: str = Field(..., description="Generated route code (KRC_xxx)")

    class Config:
        from_attributes = True


class KongRouteConfigItem(BaseModel):
    """A single Kong route config, as returned by the list endpoint."""

    code: str = Field(..., description="Route code (KRC_xxx)")
    api_name: Optional[str] = Field(None, description="API / service name")
    http_method: str = Field(..., description="HTTP method")
    route_path: str = Field(..., description="Stored Kong regex pattern (~/...$)")
    plugins: list[str] = Field(default_factory=list, description="Name-only plugins")
    regex_priority: int = Field(0, description="Kong regex_priority")
    route_group_key: Optional[str] = Field(None, description="Terragrunt group key")
    geo_loc_mst_code: Optional[str] = Field(
        None,
        description=(
            "The route's own region. Lets the Gateway tab work on a service that has "
            "no service_config row — an imported service has Kong routes but no "
            "config, so the canvas node carries no region and saving would fail."
        ),
    )
    environments_enum: Optional[str] = Field(None, description="The route's own environment")
    pending_delete: bool = Field(
        False,
        description=(
            "Row is soft-deleted but still present in the gateway — it leaves on the "
            "next deploy. Returned only when include_deleted=true, so callers can show "
            "a removal as a pending change instead of the route simply vanishing."
        ),
    )
    creation_status: Optional[DeploymentStatusEnum] = Field(None, description="Deployment status")

    class Config:
        from_attributes = True


class KongRouteConfigListResponse(BaseModel):
    """Response for listing a service's Kong routes."""

    routes: list[KongRouteConfigItem] = Field(..., description="Routes for the service")
    total: int = Field(..., description="Number of routes returned")


class KongRouteConfigDeleteResponse(BaseModel):
    """Response after (soft) deleting a Kong route config."""

    success: bool = Field(..., description="Whether the route was deleted")
    code: str = Field(..., description="Deleted route code")
    message: str = Field(..., description="Result message")


# ── Gateway state (group-keyed queue) ────────────────────────────────────────
# Nested by GROUP rather than a flat route list, because the group is now the
# unit of change: it owns the plugins, it is what a queue row points at, and its
# updated_at is the concurrency token.


class GatewayPathItem(BaseModel):
    """A live path row. Deleted paths are not here — they appear in the delta."""

    code: Optional[str] = Field(
        None,
        description=(
            "Route code (KRC_xxx). NULL for a path that exists only in an "
            "unapproved change — nothing has been written to kong_route_configs "
            "yet, so there is no row to name. Send it back as null on save and "
            "the path is created."
        ),
    )
    route_path: str = Field(..., description="Stored Kong regex pattern (~/...$)")
    creation_status: Optional[DeploymentStatusEnum] = Field(None, description="Deployment status")


class GatewayPendingChange(BaseModel):
    """
    One user's undeployed change for one group, read straight off their queue row.

    This IS the diff — nothing is recomputed to produce it. `paths` holds action
    entries ({action, code, route_path, old_path?}) exactly as the client wrote
    them, so an edit stays one entry rather than an unrelated add and delete.
    Left loosely typed: the generator and the modal read it, and pinning a model
    here would force a schema change for every new action.
    """

    queue_id: int = Field(
        ...,
        description=(
            "transaction_queue.id — what the deploy path takes as item_ids. Returned "
            "alongside the code so deploying does not need a lookup per group."
        ),
    )
    queue_code: str = Field(..., description="transaction_queue.code")
    status: str = Field(..., description="draft | approved | failed, or an in-flight status")
    in_flight: bool = Field(
        False,
        description=(
            "This change has shipped and is still running — a PR is open, or Temporal "
            "is mid-deploy. The group is READ-ONLY until it settles: editing now would "
            "be diffed against a baseline that assumes this change already landed, so "
            "if it fails its routes would be stranded. Saves against it are refused 409."
        ),
    )
    user_code: Optional[str] = Field(None, description="Who saved it — for the conflict message")
    is_mine: bool = Field(
        False,
        description=(
            "This entry belongs to the REQUESTING user. Stamped server-side because "
            "the client authenticates via Clerk and never holds its devlift user_code "
            "— without this it cannot tell its own slice from a teammate's."
        ),
    )
    delta: dict = Field(default_factory=dict, description="config_snapshot: the change set")


class GatewayGroupItem(BaseModel):
    """One Kong route object, its live paths, and its pending change if any."""

    code: Optional[str] = Field(
        None,
        description=(
            "Group code (KRG_xxx) — what a queue row points at. NULL for a group "
            "that exists only in an unapproved change: kong_route_groups is not "
            "written until deploy, so the group is described by the queue row "
            "alone. Null is also what a save sends to CREATE a group, so such a "
            "group round-trips unchanged."
        ),
    )
    route_group_key: str = Field(..., description="Terragrunt kong_configs group key")
    http_method: str = Field(..., description="HTTP method — part of the group identity")
    api_name: Optional[str] = Field(None, description="API identifier in kong_configs")
    plugins: list[str] = Field(default_factory=list, description="Name-only plugins for the group")
    regex_priority: int = Field(0, description="Kong regex_priority for the group")
    is_service_owner: bool = Field(
        False,
        description=(
            "This group carries the terragrunt service{} block. A partial deploy that "
            "omits the owner while including new groups produces HCL referencing a "
            "service block that does not exist."
        ),
    )
    updated_at: Optional[datetime] = Field(
        None,
        description=(
            "Concurrency token. Send it back on save; the UPDATE matches on it and "
            "affects 0 rows if someone else saved first, which surfaces as a 409."
        ),
    )
    paths: list[GatewayPathItem] = Field(default_factory=list)
    pending: list[GatewayPendingChange] = Field(
        default_factory=list,
        description=(
            "Undeployed changes queued for this group — one entry per user, each "
            "holding only that user's delta. Empty when the group is clean. The "
            "modal filters by user_code to show a user their own diff."
        ),
    )


class GatewayScopeItem(BaseModel):
    """One (environment, region) a service has a gateway in."""

    environment: Optional[str] = Field(None, description="Environment the groups belong to")
    geo_loc_mst_code: Optional[str] = Field(None, description="Region the groups belong to")
    group_count: int = Field(0, description="Route groups in this scope")
    path_count: int = Field(0, description="Live paths across those groups")


class GatewayScopesResponse(BaseModel):
    """
    Where a service's gateway lives, so a caller can then scope GET /gateway.

    Needed because /gateway requires an environment AND a region, and the Gateway
    tab normally reads the region off the canvas node — which comes from
    service_configs. A service imported from terragrunt has routes but no config
    row, so it had no region to send and could not open its own tab.

    A scope with a null environment or region cannot be used to call /gateway.
    It is still returned rather than hidden, so the caller can say so instead of
    silently showing a service fewer routes than it has.
    """

    service_mst_code: str = Field(..., description="Service the scopes belong to")
    scopes: list[GatewayScopeItem] = Field(default_factory=list)


class GatewayPathInput(BaseModel):
    """One path in the group's desired state."""

    code: Optional[str] = Field(
        None,
        description=(
            "Existing kong_route_configs code. None means a path that has never been "
            "written — the row is created on save."
        ),
    )
    route_path: str = Field(..., min_length=1, description="Path as the UI holds it; compiled server-side")


class GatewayPathAction(BaseModel):
    """One recorded change, relative to what is DEPLOYED."""

    action: str = Field(..., description="add | edit | delete")
    code: Optional[str] = Field(None, description="Route code; None only for 'add'")
    route_path: str = Field(..., description="Resulting path ('delete' repeats the removed one)")
    old_path: Optional[str] = Field(
        None,
        description=(
            "Required for 'edit': the DEPLOYED path being replaced — never the previous "
            "pending value. Editing a path twice before deploying keeps the original, so "
            "the generator can still find the line it has to swap."
        ),
    )

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        allowed = {"add", "edit", "delete"}
        v = (v or "").strip().lower()
        if v not in allowed:
            raise ValueError(f"action must be one of {sorted(allowed)}")
        return v


class GatewayDelta(BaseModel):
    """
    The change set stored on the queue row — read as-is by the deploy modal and
    the generator. Not derived server-side: the client owns it, because only it
    knows what was on screen when the edit began.
    """

    paths: list[GatewayPathAction] = Field(default_factory=list)
    plugins_before: list[str] = Field(default_factory=list)
    plugins_after: list[str] = Field(default_factory=list)
    # Recorded for DISPLAY and for rebuilding the deployed baseline. Generation reads
    # priority off the group, not from here — the group is the live value, this is the
    # pair needed to say "0 → 100" and to recover what was deployed.
    regex_priority_before: Optional[int] = Field(None)
    regex_priority_after: Optional[int] = Field(None)

    def is_empty(self) -> bool:
        return (
            not self.paths
            and sorted(self.plugins_before) == sorted(self.plugins_after)
            and (self.regex_priority_before or 0) == (self.regex_priority_after or 0)
        )


class GatewayGroupSave(BaseModel):
    """
    One group's save: its desired path list, plus the change record.

    Both are needed and neither is redundant. `paths` is the state to reconcile
    the TABLES to — idempotent, so saving twice or undoing a change lands
    correctly. `delta` is what gets STORED, because the server cannot derive it:
    that needs the deployed state, which it does not keep.
    """

    code: Optional[str] = Field(
        None, description="Group code (KRG_xxx). None creates the group."
    )
    route_group_key: str = Field(..., min_length=1, description="Terragrunt kong_configs group key")
    http_method: str = Field(..., min_length=1, description="HTTP method — part of the group identity")
    updated_at: Optional[datetime] = Field(
        None,
        description=(
            "Concurrency token from the GET. The UPDATE matches on it; 0 rows means "
            "someone else saved first and the request is rejected with 409. Required "
            "for an existing group — omitting it would let a stale form overwrite."
        ),
    )
    plugins: list[str] = Field(default_factory=list, description="Desired plugins for the group")
    regex_priority: int = Field(0, description="Desired regex_priority")
    paths: list[GatewayPathInput] = Field(default_factory=list, description="Desired paths")
    delta: GatewayDelta = Field(default_factory=GatewayDelta, description="Change record to store")


class GatewaySaveRequest(BaseModel):
    """Save pending gateway changes for one service, environment and region."""

    service_mst_code: str = Field(..., description="Service the groups belong to")
    application_code: Optional[str] = Field(None, description="For the workspace access check")
    environment: EnvironmentEnum = Field(..., description="Environment the groups belong to")
    geo_loc_mst_code: str = Field(..., description="Region the groups belong to")
    groups: list[GatewayGroupSave] = Field(..., min_length=1, description="Groups being saved")


class GatewaySavedGroup(BaseModel):
    """Outcome for one saved group."""

    group_code: str = Field(..., description="Group code (KRG_xxx)")
    queue_code: Optional[str] = Field(None, description="Pending queue row, or None if the change was cleared")
    updated_at: Optional[datetime] = Field(None, description="New token — the client must keep this")
    created: int = Field(0, description="Paths inserted")
    updated: int = Field(0, description="Paths updated in place")
    removed: int = Field(0, description="Paths soft-deleted")


class GatewaySaveResponse(BaseModel):
    """Response after saving gateway changes."""

    success: bool = Field(True)
    groups: list[GatewaySavedGroup] = Field(default_factory=list)


class GatewayStateResponse(BaseModel):
    """
    A service's gateway for ONE environment and region.

    Scope is resolved from kong_route_groups, not services_mst — the service
    table has no environment or region columns, so the group rows are the only
    place stage and prod are distinguishable.

    ONE region, always. When the caller does not send a region the server picks
    it, but it never merges: a service with routes in two regions has two
    terragrunt files, and save stamps NEW groups with a single region — so a
    merged response would create groups in whichever region happened to be sent
    and would need two PRs from one change set. Several candidates means none is
    chosen; `scopes` is returned so the caller can ask.
    """

    service_mst_code: str = Field(..., description="Service code")
    service_name: Optional[str] = Field(None, description="Service name")
    environment: Optional[str] = Field(None, description="Environment the groups belong to")
    geo_loc_mst_code: Optional[str] = Field(
        None,
        description=(
            "The region these groups belong to — echoed back RESOLVED. Null only "
            "when the service has no gateway in this environment, or has one in "
            "several regions and the caller sent none."
        ),
    )
    scopes: list[GatewayScopeItem] = Field(
        default_factory=list,
        description=(
            "Populated only when the caller sent no region AND more than one "
            "candidate exists. `groups` is empty in that case — pick a scope and "
            "call again with its geo_loc_mst_code."
        ),
    )
    groups: list[GatewayGroupItem] = Field(default_factory=list)
    total_groups: int = Field(0, description="Number of groups returned")
    pending_groups: int = Field(0, description="How many have an undeployed change")
