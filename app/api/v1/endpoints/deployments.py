"""
Deployment History API Endpoints

Lists DeploymentWorkflow executions from Temporal, enriched with
queue item display names plus application/environment resolved from the DB
via the DeployQueueCodes search attribute → transaction_queue join.
"""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.authz.security import AuthorizationFromBody, SecureRouter
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.transaction_queue_model import TransactionQueueModel
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.core.enum import WorkflowSourceTableEnum
from app.temporal.client import get_temporal_client, workflow_query_http_error
from app.schemas.deployment_history_schemas import (
    DeploymentHistoryItem,
    DeploymentHistoryResponse,
    DeploymentResource,
)
from app.schemas.deployment_schemas import (
    MultipleDeployRequest,
    MultipleDeployResponse,
)

router = APIRouter()

# Routes that carry their own FGA authorization live here. Kept separate from
# `router` so the remaining (not-yet-carded) deployment routes stay on the
# legacy public_surface declaration instead of failing to import.
secure_router = SecureRouter()

try:
    from temporalio.common import SearchAttributeKey as _SAKey
    _USER_KEY = _SAKey.for_keyword("DeployUserCode")
    _QUEUE_KEY = _SAKey.for_keyword("DeployQueueCodes")
    _PR_KEY = _SAKey.for_int("DeployPrNumber")
except Exception:
    _USER_KEY = _QUEUE_KEY = _PR_KEY = None


def _get_sa(wf, key):
    if key is None:
        return None
    try:
        return wf.typed_search_attributes.get(key) if wf.typed_search_attributes else None
    except Exception:
        return None


@router.get("", response_model=DeploymentHistoryResponse)
async def list_deployment_history(
    status: Optional[str] = Query(None, description="Running | Completed | Failed | TimedOut"),
    user_code: Optional[str] = Query(None, description="Filter by user code"),
    application_code: Optional[str] = Query(None, description="Filter by application/product code"),
    environment: Optional[str] = Query(None, description="Filter by environment (e.g. stage, prod)"),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    _, tenant = current_user_tenant
    client = await get_temporal_client()

    # Build Temporal query — all deployment workflows for this tenant
    # (stage/qa DeploymentWorkflow + prod ProductionDeploymentWorkflow;
    # without both, prod deploys vanish from the history list).
    parts = [
        '(WorkflowType="DeploymentWorkflow" OR WorkflowType="ProductionDeploymentWorkflow")',
        f'DeployTenantCode="{tenant.code}"',
    ]
    if status:
        parts.append(f'ExecutionStatus="{status}"')
    if user_code:
        parts.append(f'DeployUserCode="{user_code}"')

    raw_workflows = []
    async for wf in client.list_workflows(query=" AND ".join(parts)):
        raw_workflows.append(wf)
        if len(raw_workflows) >= limit:
            break

    if not raw_workflows:
        return DeploymentHistoryResponse(items=[], total=0)

    # Collect queue IDs and user codes from search attributes
    all_queue_ids: list[int] = []
    all_user_codes: list[str] = []
    for wf in raw_workflows:
        for part in (_get_sa(wf, _QUEUE_KEY) or "").split(","):
            if part.strip().isdigit():
                all_queue_ids.append(int(part.strip()))
        uc = _get_sa(wf, _USER_KEY)
        if uc:
            all_user_codes.append(uc)

    # ── Enrich: queue items (display name + status) ───────────────────────────
    queue_map: dict[int, TransactionQueueModel] = {}
    if all_queue_ids:
        result = await db.execute(
            select(TransactionQueueModel).where(TransactionQueueModel.id.in_(all_queue_ids))
        )
        for q in result.scalars():
            queue_map[q.id] = q

    # ── Enrich: application + environment per queue ID ────────────────────────
    # Two queries: one for SERVICE_CONFIG items, one for INFRASTRUCTURE items.
    # Result: queue_id → {application_code, application_name, environment}
    queue_app_map: dict[int, dict] = {}

    svc_ids = [
        q.id for q in queue_map.values()
        if q.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG and q.transaction_code
    ]
    if svc_ids:
        rows = await db.execute(
            select(
                TransactionQueueModel.id,
                ServiceConfigModel.environment,
                ServicesMstModel.applications_mst_code,
                ApplicationsMstModel.name.label("app_name"),
            )
            .join(ServiceConfigModel, ServiceConfigModel.code == TransactionQueueModel.transaction_code)
            .join(ServicesMstModel, ServicesMstModel.code == ServiceConfigModel.services_mst_code)
            .join(ApplicationsMstModel, ApplicationsMstModel.code == ServicesMstModel.applications_mst_code)
            .where(TransactionQueueModel.id.in_(svc_ids))
        )
        for row in rows:
            queue_app_map[row.id] = {
                "application_code": row.applications_mst_code,
                "application_name": row.app_name,
                "environment": row.environment.value if row.environment else None,
            }

    infra_ids = [
        q.id for q in queue_map.values()
        if q.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE and q.transaction_code
    ]
    if infra_ids:
        rows = await db.execute(
            select(
                TransactionQueueModel.id,
                InfrastructureMstModel.environments_enum,
                InfrastructureMstModel.applications_mst_code,
                ApplicationsMstModel.name.label("app_name"),
            )
            .join(InfrastructureMstModel, InfrastructureMstModel.code == TransactionQueueModel.transaction_code)
            .join(ApplicationsMstModel, ApplicationsMstModel.code == InfrastructureMstModel.applications_mst_code)
            .where(TransactionQueueModel.id.in_(infra_ids))
        )
        for row in rows:
            queue_app_map[row.id] = {
                "application_code": row.applications_mst_code,
                "application_name": row.app_name,
                "environment": row.environments_enum.value if row.environments_enum else None,
            }

    # ── Enrich: users ─────────────────────────────────────────────────────────
    user_map: dict[str, UserMstModel] = {}
    if all_user_codes:
        result = await db.execute(
            select(UserMstModel).where(UserMstModel.code.in_(list(set(all_user_codes))))
        )
        for u in result.scalars():
            user_map[u.code] = u

    # ── Build + filter response ───────────────────────────────────────────────
    items: list[DeploymentHistoryItem] = []
    for wf in raw_workflows:
        wf_user_code = _get_sa(wf, _USER_KEY)
        pr_number = _get_sa(wf, _PR_KEY)
        queue_ids_for_wf = [
            int(p.strip()) for p in (_get_sa(wf, _QUEUE_KEY) or "").split(",")
            if p.strip().isdigit()
        ]

        user = user_map.get(wf_user_code) if wf_user_code else None

        # Resolve application + environment from the first queue item that has the info
        wf_app_code = wf_app_name = wf_env = None
        for qid in queue_ids_for_wf:
            meta = queue_app_map.get(qid)
            if meta:
                wf_app_code = meta["application_code"]
                wf_app_name = meta["application_name"]
                wf_env = meta["environment"]
                break

        # Apply application / environment filters
        if application_code and wf_app_code != application_code:
            continue
        if environment and wf_env != environment:
            continue

        duration = None
        if wf.close_time and wf.start_time:
            duration = int((wf.close_time - wf.start_time).total_seconds())

        resources = [
            DeploymentResource(
                queue_id=q.id,
                queue_code=q.code,
                display_name=q.display_name or q.code,
                queue_status=q.status.value if q.status else "unknown",
                # Gateway rows live on SERVICE_CONFIG; only case_ref_code
                # ("add_route") tells the UI what kind of change this was.
                case_ref_code=q.case_ref_code,
            )
            for qid in queue_ids_for_wf
            if (q := queue_map.get(qid)) is not None
        ]

        items.append(DeploymentHistoryItem(
            workflow_id=wf.id,
            status=wf.status.name if wf.status else "UNKNOWN",
            started_at=wf.start_time,
            completed_at=wf.close_time,
            duration_seconds=duration,
            user_code=wf_user_code,
            user_name=f"{user.first_name} {user.last_name}".strip() if user else None,
            user_email=user.email_id if user else None,
            pr_number=pr_number,
            application_code=wf_app_code,
            application_name=wf_app_name,
            environment=wf_env,
            resources=resources,
        ))

    return DeploymentHistoryResponse(items=items, total=len(items))


# ── /deployments/multiple-deploy — combined variable + infra deployment ──────
# ONE call replacing the frontend's two-call chain (/project-variables/deploy
# then /transaction-queue/deploy). A DeploymentOrchestratorWorkflow acquires
# the coordinator locks for the whole journey, deploys staged variables first,
# and only on full success starts the infra DeploymentWorkflow as a child —
# so a service never redeploys before its variables are live (crash-loop
# guard), and concurrent deploys of the same service queue instead of
# interleaving. The existing single-purpose APIs are untouched.

@secure_router.post(
    "/multiple-deploy",
    response_model=MultipleDeployResponse,
    summary="Deploy staged variables and/or infra queue items as ONE orchestrated, locked deployment",
    # can_deploy is BLANKET: holding it on the service deploys everything staged
    # for it — secrets, configs, settings and kong together. Per-area access
    # (can_view/write_secret|config) gates viewing and saving, not deploying.
    # The object id is the body's service_config_code: the same
    # service_configs.code placed under a resource_group in the authz console.
    access=AuthorizationFromBody(
        permission="can_deploy",
        obj_type="service",
        field="service_config_code",
        deny_status=403,
    ),
)
async def multiple_deploy(
    request: MultipleDeployRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
) -> MultipleDeployResponse:
    """Starts a DeploymentOrchestratorWorkflow and returns immediately with its
    workflow_id (the workflow is RUNNING — queueing, if any, happens at the
    coordinator lock inside). Poll /deployments/multiple-deploy/status/{workflow_id}."""
    import logging
    import uuid
    from fastapi import HTTPException
    from app.core.config import settings
    from app.services.transaction_queue_service import validate_deployable_queue_items
    from app.temporal.workflows.deployment_orchestrator_workflow import (
        DeploymentOrchestratorWorkflow,
    )

    logger = logging.getLogger(__name__)

    if not settings.temporal_enabled:
        raise HTTPException(
            status_code=400,
            detail="Temporal is not enabled — multiple-deploy requires the Temporal deploy flow",
        )

    user, tenant = current_user_tenant

    # Approved and unmodified BEFORE the workflow starts. Once Temporal has the
    # batch, a refusal is a failed run to explain rather than a 409 the caller
    # can act on — and the activity behind it only ever skipped bad rows, so a
    # deploy of "nothing" finished as a success no one could account for.
    #
    # Only when item_ids were named: omitting them means "all of the caller's
    # approved items", which the workflow resolves itself. service_config_code
    # is the object the card authorized, so the rows are held to it — it is
    # optional on this route, and when absent the guard simply skips that step.
    if request.infra and request.infra.item_ids:
        await validate_deployable_queue_items(
            db,
            queue_ids=request.infra.item_ids,
            tenant_code=tenant.code,
            authorized_transaction_code=request.service_config_code,
        )
    workflow_id = f"multi-deploy-{tenant.code}-{uuid.uuid4().hex[:12]}"

    params = {
        "tenant_code": tenant.code,
        "user_code": user.code,
        "variables": request.variables.model_dump() if request.variables else None,
        "infra": request.infra.model_dump() if request.infra else None,
        "gateway": request.gateway.model_dump() if request.gateway else None,
        "service_config_code": request.service_config_code,
    }

    client = await get_temporal_client()
    await client.start_workflow(
        DeploymentOrchestratorWorkflow.run,
        args=[params],
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )
    logger.info(
        "DeploymentOrchestratorWorkflow started: workflow_id=%s tenant=%s vars=%s infra=%s",
        workflow_id, tenant.code, bool(request.variables), bool(request.infra),
    )
    return MultipleDeployResponse(workflow_id=workflow_id, status="started")


@router.get(
    "/multiple-deploy/status/{workflow_id}",
    summary="Query the orchestrator's end-to-end state (variables stage, infra child id, result)",
)
async def multiple_deploy_status(
    workflow_id: str,
    _: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """Returns the orchestrator's get_state: step (preparing | acquiring_locks |
    deploying_variables | deploying_infra | completed | failed), variable
    results, and the infra child workflow id (poll the existing
    /transaction-queue/deploy-status/{id} for granular infra progress)."""
    from fastapi import HTTPException
    from app.core.config import settings
    from app.temporal.workflows.deployment_orchestrator_workflow import (
        DeploymentOrchestratorWorkflow,
    )

    if not settings.temporal_enabled:
        raise HTTPException(status_code=400, detail="Temporal is not enabled")

    client = await get_temporal_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        state = await handle.query(DeploymentOrchestratorWorkflow.get_state)
        return {"workflow_id": workflow_id, **state}
    except Exception as e:
        # 404 ONLY when the workflow is genuinely gone; 503 when we could not
        # find out. The poller treats 404 as "deploy finished" and stops
        # tracking, so a blip answered with 404 ends the UI's deploy while the
        # deploy is still running. See workflow_query_http_error.
        raise workflow_query_http_error(e, workflow_id)
