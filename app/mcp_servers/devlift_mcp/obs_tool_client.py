"""Thin HTTP client from the MCP server to obs_tool's OWN REST API.

The service tools (create service, save settings draft, later submit / approve /
deploy) deliberately go over HTTP instead of calling the Python services
in-process: the permission checks for the review lane live in the route access
cards (`can_write_settings` on `service:{code}`, `can_approve`, `can_deploy`)
and in `ApprovalService.require()`, which needs a FastAPI `Request`. Calling
the routes keeps every one of those checks exactly as the web exercises them.
The resource path (S3 / SQS / DynamoDB) stays in-process — nothing there is
carded.

Identity: the caller's HS256 `mcp_internal` JWT (see `_internal_jwt.py`), which
`get_current_user_and_tenant` accepts and which therefore resolves to the same
`user:{user_code}` subject OpenFGA checks against.
"""

import logging
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(60.0, connect=5.0)


class ObsToolAPIError(Exception):
    """A non-2xx answer from obs_tool's REST API, with the parsed detail."""

    def __init__(self, status_code: int, detail: Any, path: str):
        self.status_code = status_code
        self.detail = detail
        self.path = path
        super().__init__(f"{path} -> {status_code}: {detail}")

    @property
    def detail_text(self) -> str:
        """Human-readable detail; dict details (409 with config_code) flattened."""
        if isinstance(self.detail, dict):
            return str(self.detail.get("message") or self.detail.get("detail") or self.detail)
        return str(self.detail)


def _base_url() -> str:
    return settings.mcp_self_api_base_url.rstrip("/")


def _parse_detail(resp: httpx.Response) -> Any:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return body


async def _post(path: str, *, jwt_token: str, json: Optional[dict] = None) -> dict:
    url = f"{_base_url()}{path}"
    headers = {"Authorization": f"Bearer {jwt_token}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=json, headers=headers)
    if resp.status_code >= 300:
        raise ObsToolAPIError(resp.status_code, _parse_detail(resp), path)
    try:
        return resp.json()
    except ValueError:
        return {}


async def _get(path: str, *, jwt_token: str, params: Optional[dict] = None) -> dict:
    url = f"{_base_url()}{path}"
    headers = {"Authorization": f"Bearer {jwt_token}"}
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=clean, headers=headers)
    if resp.status_code >= 300:
        raise ObsToolAPIError(resp.status_code, _parse_detail(resp), path)
    try:
        return resp.json()
    except ValueError:
        return {}


# Review-lane verbs and which of them carry a comment body. Discard takes no
# body at all; reject / request-changes REQUIRE a comment (RequiredCommentIn),
# the rest accept an optional one (DecisionIn).
APPROVAL_VERBS_WITH_COMMENT = frozenset(
    {"submit", "withdraw", "approve", "revoke", "reject", "request-changes"}
)
APPROVAL_VERBS = APPROVAL_VERBS_WITH_COMMENT | {"discard", "deploy"}


async def approval_action(
    *, jwt_token: str, queue_code: str, verb: str, comment: Optional[str] = None
) -> dict:
    """`POST /approvals/{queue_code}/{verb}` → ActionResponse.

    The route runs the lane's own rules (state, ownership, OpenFGA permission)
    and answers 403 / 404 / 409 with a user-facing detail. `approval` is None
    when the row no longer exists (a discard).
    """
    if verb not in APPROVAL_VERBS:
        raise ValueError(f"unknown approval verb: {verb}")
    body = {"comment": comment or ""} if verb in APPROVAL_VERBS_WITH_COMMENT else None
    return await _post(f"/approvals/{queue_code}/{verb}", jwt_token=jwt_token, json=body)


async def list_approvals(
    *, jwt_token: str, status: Optional[str] = None, resource_code: Optional[str] = None
) -> dict:
    """`GET /approvals` → {approvals: [ApprovalItem], total}.

    Drafts are private to their author, so `status=draft` is exactly the
    caller's own drafts; `submit` / `approved` list what they may act on.
    """
    return await _get(
        "/approvals", jwt_token=jwt_token, params={"status": status, "resource_code": resource_code}
    )


async def get_approval(*, jwt_token: str, queue_code: str) -> dict:
    """`GET /approvals/{queue_code}` → ApprovalItem (full snapshot, frozen
    diff, history). Visible only to callers who may approve it; others get
    404, which is deliberate on the server side."""
    return await _get(f"/approvals/{queue_code}", jwt_token=jwt_token)


async def get_service_config(*, jwt_token: str, service_config_code: str) -> dict:
    """`GET /service-configs/by-code/{code}` → ServiceConfigResponse.

    Guarded by `can_view_settings` on `service:{code}` before the handler runs.
    """
    return await _get(f"/service-configs/by-code/{service_config_code}", jwt_token=jwt_token)


async def create_service(*, jwt_token: str, payload: dict) -> dict:
    """`POST /services/create-service` → CreateServiceResponse (has `service_code`)."""
    return await _post("/services/create-service", jwt_token=jwt_token, json=payload)


async def create_service_config(*, jwt_token: str, payload: dict) -> dict:
    """`POST /service-configs` → ServiceConfigResponse (has `code`).

    This is the route the web's "Create & Add" uses. It writes the live
    service_configs row, assigns ingress_group_order, and maps the new config
    under its resource group in OpenFGA (non-fatal on that side). A 409 means
    the (service, env, geo, alb, vendor, type, cluster) identity already has a
    config; its detail carries `config_code` when the service layer raised it.
    """
    return await _post("/service-configs", jwt_token=jwt_token, json=payload)


async def save_settings_draft(
    *,
    jwt_token: str,
    service_config_code: str,
    config_snapshot: dict,
    case_ref_code: str = "update_service",
    queue_code: Optional[str] = None,
    ticket_code: Optional[str] = None,
) -> dict:
    """`POST /transaction/service-settings/{code}` → ActionResponse.

    Guarded by `can_write_settings` on `service:{code}` BEFORE the handler runs.
    Parks the change as a DRAFT queue row; writes nothing live. `approval` is
    None when the values match what is deployed (nothing queued).
    """
    body: dict = {"config_snapshot": config_snapshot, "case_ref_code": case_ref_code}
    if queue_code:
        body["queue_code"] = queue_code
    if ticket_code:
        body["ticket_code"] = ticket_code
    return await _post(
        f"/transaction/service-settings/{service_config_code}",
        jwt_token=jwt_token,
        json=body,
    )


async def get_gateway_state(*, jwt_token: str, service_config_code: str) -> dict:
    """`GET /kong-route-configs/gateway/by-config/{code}` → GatewayStateResponse.

    The Gateway tab's read: every route group of the service in that
    environment / region with its live paths and each user's pending change.
    Guarded by `can_view_gateway` on `service:{code}`.
    """
    return await _get(f"/kong-route-configs/gateway/by-config/{service_config_code}", jwt_token=jwt_token)


async def save_gateway_draft(*, jwt_token: str, service_config_code: str, gateway_groups: list) -> dict:
    """`POST /transaction/kong-gateway/{code}` → ActionResponse.

    The gateway half of the web's Save. Guarded by `can_write_gateway` on
    `service:{code}`. Parks the groups as the caller's `add_route` DRAFT row
    for that scope (one row per user), sharing the change set with the
    settings row so submit / approve / deploy move both. obs_tool corrects the
    delta against the live Kong tables; `approval` is None when nothing is
    left to change.
    """
    return await _post(
        f"/transaction/kong-gateway/{service_config_code}",
        jwt_token=jwt_token,
        json={"gateway_groups": gateway_groups},
    )


async def multiple_deploy(
    *,
    jwt_token: str,
    service_config_code: str,
    item_ids: list,
    include_gateway: bool,
) -> dict:
    """`POST /deployments/multiple-deploy` → {workflow_id, status}.

    The web's second deploy call, with the body it builds for an EKS service:
    the approved settings row's queue id under `infra`, and — when the change
    set has approved routes — `gateway.transaction_code` so obs_tool resolves
    the approved add_route row itself (never its id). Guarded by `can_deploy`
    on `service:{service_config_code}`; starts one Temporal orchestrator whose
    id is the key `get_deployment_status` polls.
    """
    body: dict = {"service_config_code": service_config_code}
    if item_ids:
        body["infra"] = {"item_ids": list(item_ids)}
    if include_gateway:
        body["gateway"] = {"transaction_code": service_config_code}
    return await _post("/deployments/multiple-deploy", jwt_token=jwt_token, json=body)
