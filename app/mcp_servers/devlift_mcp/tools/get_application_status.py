"""get_application_status tool — is the deployed application actually running?

get_deployment_status answers a different question: did DevLift's own pipeline
finish. That turns green when the infra apply and the manifest merge are done,
which is before ArgoCD has looked at the change and well before any pod is
serving. This tool asks the cluster instead, through ArgoCD.

The gap between the two is the whole problem. After a deploy completes, ArgoCD
still reports the PREVIOUS revision as Healthy for a while — it polls git every
10s, then rolls pods, and for new code the image is built by GitHub Actions
outside DevLift entirely. Reporting that stale Healthy as "your app is up"
would be wrong, so every reading is compared against the deploy's finish time:
Argo activity older than the deploy means Argo has not acted yet.

What it cannot do: prove the NEW image is live. The tag comes from the app
repo's build and DevLift never learns it, so the running image is reported for
the user to judge rather than asserted.

EKS services only. Everything else has no Argo Application and returns
not_applicable rather than a scary-looking unknown.
"""

import logging
import os
from datetime import datetime, timezone

from app.db.session import AsyncSessionLocal
from app.integrations.argocd_integration import (
    DEFAULT_APP_NAMESPACE,
    ArgoCDError,
    ArgoCDIntegration,
)
from app.mcp_servers.devlift_mcp.auth import get_auth_context
from app.mcp_servers.devlift_mcp.meta_data import is_paas_tenant
from app.mcp_servers.devlift_mcp.service_payloads import eks_namespace_for
from app.mcp_servers.devlift_mcp.session_cache import scan_user_drafts
from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
from app.repository.service_config_repository import ServiceConfigRepository
from app.utils.service_routing import display_service_path
from app.utils.service_urls import build_health_url
from app.utils.tenant_config import get_tenant_config

logger = logging.getLogger(__name__)

# How long after the deploy finished we keep waiting for ArgoCD before handing
# the user the link and stopping. A config change with the image already in ECR
# settles in a minute or two; a first deploy waits on a GitHub Actions build,
# which is why this is not tighter.
POLL_WINDOW_SECONDS = 300

_TERMINAL_RUN_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}
_ARGO_SERVICE_TYPES = {"eks_service"}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest(*values: str | None) -> datetime | None:
    stamps = [ts for ts in (_parse_ts(v) for v in values) if ts is not None]
    return max(stamps) if stamps else None


async def _deploy_finished_at(db, workflow_id: str | None) -> datetime | None:
    """When DevLift's own deploy reached a terminal state, or None.

    None means this is a standalone "is it up?" question with no deploy to
    measure against, so the staleness check is skipped rather than guessed.
    """
    if not workflow_id:
        return None
    try:
        rows = await PipelineRunTrackRepository(db).get_by_vendor_deployment_id(workflow_id)
    except Exception:
        logger.exception("argocd app status: run track lookup failed for %s", workflow_id)
        return None

    finished = []
    for row in rows or []:
        status = row.status.value if hasattr(row.status, "value") else str(row.status or "")
        if status.upper() in _TERMINAL_RUN_STATUSES and row.updated_at:
            finished.append(row.updated_at)
    if not finished:
        return None
    latest = max(finished)
    return latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)


def _describe(
    *,
    identifier: str,
    health: str | None,
    sync: str | None,
    stale: bool,
    expired: bool,
) -> tuple[str, str, bool]:
    """(state, message, settled) for one service."""
    if stale:
        if expired:
            return (
                "not_picked_up",
                f"ArgoCD has not picked up the change for '{identifier}' yet. Open ArgoCD and check.",
                True,
            )
        return (
            "waiting_for_argocd",
            f"'{identifier}': waiting for ArgoCD to pick up the change.",
            False,
        )

    if health == "Healthy":
        return ("up", f"'{identifier}' is up and healthy.", True)

    if health == "Degraded":
        if expired:
            return (
                "unhealthy",
                f"'{identifier}' is not healthy. Open ArgoCD and check what failed.",
                True,
            )
        # Pods flicker Degraded mid-rollout and recover. Shown every tick, but
        # not called a failure until it has had the full window to settle.
        return ("unhealthy", f"'{identifier}' is unhealthy right now — still watching.", False)

    if health in ("Progressing", None):
        if expired:
            return (
                "starting",
                f"'{identifier}' is still rolling out. Open ArgoCD to follow it.",
                True,
            )
        return ("starting", f"'{identifier}' is starting up.", False)

    if health == "Missing":
        return ("missing", f"'{identifier}' has no running workload in the cluster.", True)

    if health == "Suspended":
        return ("suspended", f"'{identifier}' is paused in ArgoCD.", True)

    label = f"{health} / {sync}" if sync else str(health)
    return ("unknown", f"'{identifier}': ArgoCD reports {label}.", True)


async def _status_for_draft(db, *, tenant_code: str, draft: dict) -> dict | None:
    identifier = draft.get("identifier") or "this service"
    service_config_code = draft.get("transaction_code")
    if not service_config_code:
        return None

    row = await ServiceConfigRepository(db).get_by_code_and_tenant(service_config_code, tenant_code)
    if not row:
        return None

    config = row.config if isinstance(row.config, dict) else {}
    environment = row.environment.value if hasattr(row.environment, "value") else str(row.environment or "")

    settings = (await get_tenant_config(tenant_code, db)).argocd_for(environment)
    host = (settings.get("host") or "").strip()
    token = os.getenv((settings.get("token_env") or "").strip(), "")
    if not host or not token:
        return {
            "identifier": identifier,
            "environment": environment,
            "state": "not_configured",
            "message": f"ArgoCD is not set up for {environment}, so live status is unavailable.",
            "settled": True,
        }

    # `identifier` is the name the user deployed under, which is what the Argo
    # Application is named. service_configs.name is a generated label
    # ("Config for <uuid> - stage - ...") and is never a service name.
    app_name = eks_namespace_for(config.get("service_name") or identifier)
    namespace = settings.get("app_namespace") or DEFAULT_APP_NAMESPACE
    client = ArgoCDIntegration(host=host, token=token)

    try:
        app = await client.get_application(app_name, namespace)
    except ArgoCDError as e:
        logger.warning("argocd app status: %s unreachable: %s", host, e)
        return {
            "identifier": identifier,
            "environment": environment,
            "state": "unavailable",
            "message": f"Could not reach ArgoCD to check '{identifier}'.",
            "settled": True,
        }

    health_url = build_health_url(
        config.get("alb_url") or "",
        config.get("health") or "",
        display_service_path(config),
    )
    argocd_url = client.application_url(app_name, namespace)

    if not app:
        return {
            "identifier": identifier,
            "environment": environment,
            "state": "not_found",
            "message": f"'{identifier}' has no application in ArgoCD yet.",
            "argocd_url": argocd_url,
            "settled": True,
        }

    deployed_at = await _deploy_finished_at(db, draft.get("workflow_id"))
    argo_activity = _latest(
        app.get("sync_finished_at"), app.get("health_changed_at"), app.get("reconciled_at")
    )
    stale = bool(deployed_at and (argo_activity is None or argo_activity < deployed_at))
    expired = bool(
        deployed_at
        and (datetime.now(timezone.utc) - deployed_at).total_seconds() > POLL_WINDOW_SECONDS
    )

    state, message, settled = _describe(
        identifier=identifier,
        health=app.get("health"),
        sync=app.get("sync"),
        stale=stale,
        expired=expired,
    )

    return {
        "identifier": identifier,
        "environment": environment,
        "state": state,
        "message": message,
        "sync_status": app.get("sync"),
        "health_status": app.get("health"),
        "running_image": app["images"][0] if app.get("images") else None,
        "argocd_url": argocd_url,
        "health_url": health_url or None,
        "settled": settled,
    }


async def get_application_status_impl(
    project_id: str | None = None,
    service_name: str | None = None,
) -> dict:
    """Live ArgoCD health for the user's recently deployed EKS services."""
    auth_ctx = await get_auth_context()
    if not auth_ctx:
        return {
            "status": "error",
            "message": "Authentication required — please run the 'authenticate' tool first.",
        }

    if is_paas_tenant(auth_ctx.tenant_code):
        return {
            "status": "not_applicable",
            "message": "Live application status is not available for this account.",
            "applications": [],
        }

    drafts = [
        d for d in await scan_user_drafts(auth_ctx.user_code)
        if d.get("resource_type") in _ARGO_SERVICE_TYPES
    ]
    if project_id is not None:
        drafts = [d for d in drafts if d.get("project_id") == project_id]
    if service_name:
        wanted = service_name.strip().lower()
        drafts = [d for d in drafts if (d.get("identifier") or "").strip().lower() == wanted]

    if not drafts:
        return {
            "status": "no_applications",
            "message": "No EKS service from this conversation to check.",
            "applications": [],
        }

    # One entry per service — a service redeployed in the same conversation has
    # several drafts, and only its current state is worth reporting.
    by_code: dict[str, dict] = {}
    for draft in drafts:
        code = draft.get("transaction_code")
        if not code:
            continue
        current = by_code.get(code)
        if current is None or (draft.get("last_updated") or "") > (current.get("last_updated") or ""):
            by_code[code] = draft

    applications = []
    async with AsyncSessionLocal() as db:
        for draft in by_code.values():
            try:
                result = await _status_for_draft(db, tenant_code=auth_ctx.tenant_code, draft=draft)
            except Exception:
                logger.exception("argocd app status failed for draft %s", draft.get("draft_id"))
                result = None
            if result:
                applications.append(result)

    if not applications:
        return {
            "status": "no_applications",
            "message": "No EKS service from this conversation to check.",
            "applications": [],
        }

    all_settled = all(a["settled"] for a in applications)

    if all_settled:
        next_action = {
            "type": "present_completion",
            "instruction": (
                "Stop polling. Show each `message` verbatim, one line per "
                "service. When `health_url` is set, offer it as a clickable "
                "link the user can open to check the app themselves. Present "
                "`argocd_url` as a link whenever the state is not `up`. "
                "Mention `running_image` only if the user asks which version "
                "is live."
            ),
        }
    else:
        next_action = {
            "type": "continue_polling",
            "instruction": (
                "Not settled yet. Show each `message` (one line per service) "
                "and let `/loop` call get_application_status again. Do not "
                "declare success or failure while this is the next_action."
            ),
        }

    return {
        "status": "ok",
        "all_settled": all_settled,
        "next_action": next_action,
        "applications": applications,
    }
