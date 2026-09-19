"""
Deployment History (DB-backed) API Endpoint

Lists deployment runs from the database (pipeline_mst + pipeline_run_track)
rather than from Temporal. Every Temporal deploy writes one pipeline_run_track
row per resource, all sharing a single vendor_deployment_id (the Temporal
workflow_id) — so one deployment = the group of run rows with the same
vendor_deployment_id. Rows are enriched with queue item display names and the
application/environment resolved via the transaction_queue → service_config /
infrastructure_mst join. Temporal still *runs* deployments; this endpoint no
longer depends on it for the listing.

Two routes:
  GET ""               — paginated list. Filters (status/env/app/geo/vendor/
                         search/date window/deploy kind) and ordering all run
                         in Postgres, so `total` and page boundaries are
                         computed over the filtered set. Deliberately lean:
                         no build_stages on list rows.
  GET "/{workflow_id}" — one deployment with its merged pipeline stages, for
                         the Admin Dashboard's detail modal (fetched lazily on
                         open, polled while RUNNING).

Kept in its own module (mounted at /deployments/history) so the shared
deployments.py — whose deploy-path routes are maintained separately — stays
untouched.
"""
from collections import OrderedDict
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.transaction_queue_model import TransactionQueueModel
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.geo_loc_mst_model import GeoLocMstModel
from app.db.models.infra_vendor_accounts_mst_model import InfraVendorAccountsMstModel
from app.db.models.kong_route_config_model import KongRouteConfigModel
from app.db.models.kong_route_group_model import KongRouteGroupModel
from app.db.models.pipeline_mst_model import PipelineMstModel
from app.db.models.pipeline_run_track_model import PipelineRunTrackModel
from app.core.enum import WorkflowSourceTableEnum, PipelineRunStatusEnum
from app.schemas.deployment_history_schemas import (
    DeploymentHistoryDetail,
    DeploymentHistoryItem,
    DeploymentHistoryResponse,
    DeploymentResource,
    DeploymentStage,
)

router = APIRouter()


# Per-run SQL predicates for the three deploy kinds the platform ships
# (gateway routes / service settings / variables & secrets) plus raw infra.
# The queue items deployed in a run live in rt.transaction_queue_code (JSONB
# array of transaction_queue codes); a multi-deploy anchors ALL of them under
# one SERVICE_CONFIG pipeline, so the run's own pipeline_mst.table_name is not
# enough — the per-item case_ref/table_name is what identifies the kind.
# Variables-only runs have no queue rows at all, hence the build_stages
# fallback (same '{artifact}: {action}' convention as _is_variables_only).
# Shared with admin_deploy_tracker.py so both surfaces classify identically.
# jsonb_array_elements* throws "cannot extract elements from a scalar" on
# legacy rows whose JSONB holds a scalar/object instead of an array — and
# COALESCE only covers SQL NULL, not JSON scalars. jsonb_typeof guards both.
_TQ_CODES_ARRAY = (
    "(CASE WHEN jsonb_typeof(rt.transaction_queue_code) = 'array'"
    " THEN rt.transaction_queue_code ELSE '[]'::jsonb END)"
)
_BUILD_STAGES_ARRAY = (
    "(CASE WHEN jsonb_typeof(rt.build_stages) = 'array'"
    " THEN rt.build_stages ELSE '[]'::jsonb END)"
)
_QUEUE_EXISTS = (
    f"EXISTS (SELECT 1 FROM jsonb_array_elements_text({_TQ_CODES_ARRAY}) tc(code)"
    " JOIN transaction_queue tq2 ON tq2.code = tc.code WHERE {cond})"
)
DEPLOY_TYPE_SQL: dict[str, str] = {
    "gateway": (
        "(p.table_name::text = 'KONG_ROUTE' OR "
        + _QUEUE_EXISTS.format(cond="tq2.table_name::text = 'KONG_ROUTE' OR tq2.case_ref_code = 'add_route'")
        + ")"
    ),
    "settings": _QUEUE_EXISTS.format(
        cond="tq2.case_ref_code = 'update_service'"
        " OR (tq2.table_name::text IN ('SERVICE_CONFIG','SERVICE_CONFIG_DOCKERFILE')"
        " AND COALESCE(tq2.case_ref_code, '') NOT IN ('update_variables','add_route'))"
    ),
    "variables": (
        "("
        + _QUEUE_EXISTS.format(cond="tq2.case_ref_code = 'update_variables'")
        + f" OR EXISTS (SELECT 1 FROM jsonb_array_elements({_BUILD_STAGES_ARRAY}) st"
        "  WHERE lower(st->>'name') LIKE 'secrets:%' OR lower(st->>'name') LIKE 'configs:%'))"
    ),
    "infra": (
        "(p.table_name::text = 'INFRASTRUCTURE' OR "
        + _QUEUE_EXISTS.format(cond="tq2.table_name::text = 'INFRASTRUCTURE'")
        + ")"
    ),
}

# Whitelisted ORDER BY expressions over the grouped CTE — sort must happen
# server-side (before OFFSET/LIMIT) or page N is meaningless.
_SORT_EXPRS: dict[str, str] = {
    "started_at": "g.last_created",
    "duration": "g.duration_secs",
    "status": "g.agg_status",
    "environment": "g.env_val",
    "service": "g.service_val",
}


def _aggregate_status(runs: list[PipelineRunTrackModel]) -> str:
    """A deployment groups several run rows; surface the worst/most-in-flight
    status: any failure → FAILED, else any timeout → TIMED_OUT, else anything
    still running/pending → RUNNING, else COMPLETED."""
    statuses = {r.status for r in runs}
    if PipelineRunStatusEnum.FAILED in statuses or PipelineRunStatusEnum.CANCELLED in statuses:
        return "FAILED"
    if PipelineRunStatusEnum.TIMEOUT in statuses:
        return "TIMED_OUT"
    if PipelineRunStatusEnum.RUNNING in statuses or PipelineRunStatusEnum.PENDING in statuses:
        return "RUNNING"
    return "COMPLETED"


# Stage names written by the variables activity ('{artifact}: {action}' convention).
_VARIABLE_STAGE_PREFIXES = ("secrets:", "configs:")
# Orchestrator stages present on every run — not artifact work, so ignore them.
_GENERIC_STAGES = {"waiting in queue", "completed"}


def _is_variables_only(runs: list[PipelineRunTrackModel]) -> bool:
    """True when a run's recorded stages are variable work only (secrets/configs
    saves) and no infra/k8s/service stages — i.e. an env-only update.

    Read from build_stages rather than inferring from an empty
    transaction_queue_code: a combined variables+infra deploy writes its variable
    stages onto the same rows as the infra ones, so 'has variable stages AND no
    other artifact stages' is what actually identifies an env-only run.
    """
    saw_variable_stage = False
    for run in runs:
        for stage in (run.build_stages or []):
            name = (stage.get("name") or "").strip().lower()
            if not name or name in _GENERIC_STAGES:
                continue
            if name.startswith(_VARIABLE_STAGE_PREFIXES):
                saw_variable_stage = True
            else:
                return False  # any other artifact stage ⇒ not env-only
    return saw_variable_stage


def _extract_pr(runs: list[PipelineRunTrackModel]) -> tuple[Optional[int], Optional[str]]:
    """Pull the PR number + url from deploy_result.prs (written by the workflow
    as [{name, url, repo, number}], INFRA PR first). Scan newest run first."""
    for run in runs:
        dr = run.deploy_result or {}
        for pr in (dr.get("prs") or []):
            num = pr.get("number")
            if isinstance(num, int):
                return num, pr.get("url")
    return None, None


def _collect_stages(runs: list[PipelineRunTrackModel]) -> list[DeploymentStage]:
    """Merge build_stages across a deployment's run rows into one timeline.

    The stage writers append the SAME stage entry onto every run row sharing
    the vendor_deployment_id, so dedupe on (name, started_at). Entries without
    a name or start time can't be rendered on a timeline — skip them.
    """
    seen: set[tuple[str, str]] = set()
    stages: list[DeploymentStage] = []
    for run in runs:
        for st in (run.build_stages or []):
            name = st.get("name")
            started_at = st.get("started_at")
            if not name or not started_at or (name, started_at) in seen:
                continue
            seen.add((name, started_at))
            stages.append(DeploymentStage(
                name=name,
                status=st.get("status") or "completed",
                started_at=started_at,
                ended_at=st.get("ended_at"),
                error=st.get("error"),
            ))
    stages.sort(key=lambda s: s.started_at)
    return stages


async def _build_deployment_items(
    db: AsyncSession, page_vids: list[str]
) -> tuple[list[DeploymentHistoryItem], "OrderedDict[str, dict]"]:
    """Fetch + enrich the given deployments (by vendor_deployment_id), in the
    given order. Shared by the list page and the single-deployment detail.
    Returns the items plus the raw per-vid groups ({"runs": [...], ...}) so
    the detail route can also read build_stages off the runs."""
    rows_result = await db.execute(
        select(
            PipelineRunTrackModel,
            PipelineMstModel.transaction_code,
            PipelineMstModel.table_name,
        )
        .join(PipelineMstModel, PipelineMstModel.code == PipelineRunTrackModel.pipeline_mst_code)
        .where(
            PipelineRunTrackModel.vendor_deployment_id.in_(page_vids),
            PipelineRunTrackModel.is_deleted == False,  # noqa: E712
        )
    )
    rows = rows_result.all()
    if not rows:
        return [], OrderedDict()

    # ── Group rows into deployments by vendor_deployment_id (order preserved) ─
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for run, transaction_code, table_name in rows:
        g = groups.setdefault(run.vendor_deployment_id, {"runs": [], "resources": [], "tx": []})
        g["runs"].append(run)
        tn = table_name.value if hasattr(table_name, "value") else table_name
        # The resource this run targets — always present, even when no queue item
        # was recorded (e.g. a deploy that failed early). Used for env/app/geo
        # resolution so display matches what Phase 1 filtered on.
        if transaction_code and (transaction_code, tn) not in g["tx"]:
            g["tx"].append((transaction_code, tn))
        # Queue items deployed (may be empty for failed-early deploys).
        for qcode in (run.transaction_queue_code or []):
            g["resources"].append({
                "queue_code": qcode,
                "transaction_code": transaction_code,
                "table_name": tn,
            })

    # ── Enrich: queue items (display name + status) by queue code ─────────────
    all_queue_codes = {
        r["queue_code"]
        for g in groups.values() for r in g["resources"] if r["queue_code"]
    }
    queue_map: dict[str, TransactionQueueModel] = {}
    if all_queue_codes:
        result = await db.execute(
            select(TransactionQueueModel).where(TransactionQueueModel.code.in_(all_queue_codes))
        )
        for q in result.scalars():
            queue_map[q.code] = q

    # ── Enrich: who triggered it — run rows carry no user, but every queue
    # item does (transaction_queue.user_code → user_mst). A deployment's user
    # is taken from its first queue item that resolves to a known user.
    #
    # Variables-only runs are the gap: their run rows link NO queue items,
    # yet the Env & Permissions save DID record an update_variables row for
    # the service — user_code already on it. Resolve those by the run's own
    # transaction_code (which every run has), newest row per service. This is
    # read-side only, so old "unknown" deployments attribute retroactively.
    unattributed_tx = {
        tc
        for g in groups.values()
        if not any(
            (q := queue_map.get(r["queue_code"])) and q.user_code
            for r in g["resources"]
        )
        for (tc, tn) in g["tx"]
        if tn == WorkflowSourceTableEnum.SERVICE_CONFIG.value and tc
    }
    variables_user_by_tx: dict[str, str] = {}
    if unattributed_tx:
        var_rows = await db.execute(
            select(TransactionQueueModel)
            .where(
                TransactionQueueModel.transaction_code.in_(unattributed_tx),
                TransactionQueueModel.case_ref_code == "update_variables",
                TransactionQueueModel.user_code.isnot(None),
                TransactionQueueModel.is_deleted == False,  # noqa: E712
            )
            .order_by(TransactionQueueModel.created_at.desc())
        )
        for q in var_rows.scalars():
            variables_user_by_tx.setdefault(q.transaction_code, q.user_code)

    user_map: dict[str, UserMstModel] = {}
    queue_user_codes = {q.user_code for q in queue_map.values() if q.user_code}
    queue_user_codes |= set(variables_user_by_tx.values())
    if queue_user_codes:
        user_rows = await db.execute(
            select(UserMstModel).where(UserMstModel.code.in_(queue_user_codes))
        )
        for u in user_rows.scalars():
            user_map[u.code] = u

    # ── Enrich: application + environment per transaction_code ────────────────
    # Two queries: SERVICE_CONFIG items and INFRASTRUCTURE items.
    # Result: transaction_code → {application_code, application_name, environment}
    tx_app_map: dict[str, dict] = {}
    all_tx = [tx for g in groups.values() for tx in g["tx"]]

    # Also resolve each queue item's OWN resource: older queue rows predate
    # display_name being written at save time, and their readable fallback is
    # the resource's name — without this they render as raw "queue-…" codes.
    for q in queue_map.values():
        if q.transaction_code and q.table_name:
            pair = (q.transaction_code, q.table_name.value)
            if pair not in all_tx:
                all_tx.append(pair)

    svc_codes = {
        tc for (tc, tn) in all_tx
        if tn == WorkflowSourceTableEnum.SERVICE_CONFIG.value and tc
    }
    if svc_codes:
        svc_rows = await db.execute(
            select(
                ServiceConfigModel.code,
                ServiceConfigModel.environment,
                ServiceConfigModel.geo_loc_mst_code,
                ServiceConfigModel.infra_vendor_enum,
                ServiceConfigModel.infrastructuretype_ref_code,
                ServicesMstModel.applications_mst_code,
                ServicesMstModel.name.label("resource_name"),
                ApplicationsMstModel.name.label("app_name"),
                GeoLocMstModel.name.label("geo_name"),
            )
            .outerjoin(ServicesMstModel, ServicesMstModel.code == ServiceConfigModel.services_mst_code)
            .outerjoin(ApplicationsMstModel, ApplicationsMstModel.code == ServicesMstModel.applications_mst_code)
            .outerjoin(GeoLocMstModel, GeoLocMstModel.code == ServiceConfigModel.geo_loc_mst_code)
            .where(ServiceConfigModel.code.in_(svc_codes))
        )
        for row in svc_rows:
            tx_app_map[row.code] = {
                "application_code": row.applications_mst_code,
                "application_name": row.app_name,
                "environment": row.environment.value if row.environment else None,
                "geo_code": row.geo_loc_mst_code,
                "geo_name": row.geo_name,
                "vendor": row.infra_vendor_enum.value if row.infra_vendor_enum else None,
                "name": row.resource_name,
                "resource_type": row.infrastructuretype_ref_code,
            }

    infra_codes = {
        tc for (tc, tn) in all_tx
        if tn == WorkflowSourceTableEnum.INFRASTRUCTURE.value and tc
    }
    if infra_codes:
        infra_rows = await db.execute(
            select(
                InfrastructureMstModel.code,
                InfrastructureMstModel.environments_enum,
                InfrastructureMstModel.geo_loc_mst_code,
                InfrastructureMstModel.infrastructuretype_ref_code,
                InfraVendorAccountsMstModel.infra_vendor_enum,
                InfrastructureMstModel.applications_mst_code,
                InfrastructureMstModel.name.label("resource_name"),
                ApplicationsMstModel.name.label("app_name"),
                GeoLocMstModel.name.label("geo_name"),
            )
            .outerjoin(ApplicationsMstModel, ApplicationsMstModel.code == InfrastructureMstModel.applications_mst_code)
            .outerjoin(GeoLocMstModel, GeoLocMstModel.code == InfrastructureMstModel.geo_loc_mst_code)
            .outerjoin(
                InfraVendorAccountsMstModel,
                InfraVendorAccountsMstModel.code == InfrastructureMstModel.infra_vendor_accounts_mst_code,
            )
            .where(InfrastructureMstModel.code.in_(infra_codes))
        )
        for row in infra_rows:
            tx_app_map[row.code] = {
                "application_code": row.applications_mst_code,
                "application_name": row.app_name,
                "environment": row.environments_enum.value if row.environments_enum else None,
                "geo_code": row.geo_loc_mst_code,
                "geo_name": row.geo_name,
                "vendor": row.infra_vendor_enum.value if row.infra_vendor_enum else None,
                "name": row.resource_name,
                "resource_type": row.infrastructuretype_ref_code,
            }

    # Kong routes carry env + geo (+ app via services_mst); no cloud vendor.
    # services_mst_code is nullable (global/plugin routes), so all joins are outer.
    kong_codes = {
        tc for (tc, tn) in all_tx
        if tn == WorkflowSourceTableEnum.KONG_ROUTE.value and tc
    }
    if kong_codes:
        kong_rows = await db.execute(
            select(
                KongRouteConfigModel.code,
                KongRouteConfigModel.environments_enum,
                KongRouteConfigModel.geo_loc_mst_code,
                KongRouteConfigModel.api_name.label("resource_name"),
                ServicesMstModel.applications_mst_code,
                ApplicationsMstModel.name.label("app_name"),
                GeoLocMstModel.name.label("geo_name"),
            )
            .outerjoin(ServicesMstModel, ServicesMstModel.code == KongRouteConfigModel.services_mst_code)
            .outerjoin(ApplicationsMstModel, ApplicationsMstModel.code == ServicesMstModel.applications_mst_code)
            .outerjoin(GeoLocMstModel, GeoLocMstModel.code == KongRouteConfigModel.geo_loc_mst_code)
            .where(KongRouteConfigModel.code.in_(kong_codes))
        )
        for row in kong_rows:
            tx_app_map[row.code] = {
                "application_code": row.applications_mst_code,
                "application_name": row.app_name,
                "environment": row.environments_enum.value if row.environments_enum else None,
                "geo_code": row.geo_loc_mst_code,
                "geo_name": row.geo_name,
                "vendor": None,
                "name": row.resource_name,
            }

    # Gateway-tab deploys point their KONG_ROUTE runs at a route GROUP (KRG_*)
    # rather than a single route, so resolve whatever the per-route query above
    # missed against kong_route_groups — mirroring the kg join in Phase 1.
    # Without this a gateway deploy passes the filters but renders with its raw
    # code and null app/env/region.
    kong_group_codes = kong_codes - tx_app_map.keys()
    if kong_group_codes:
        kong_group_rows = await db.execute(
            select(
                KongRouteGroupModel.code,
                KongRouteGroupModel.environments_enum,
                KongRouteGroupModel.geo_loc_mst_code,
                KongRouteGroupModel.api_name.label("resource_name"),
                ServicesMstModel.applications_mst_code,
                ApplicationsMstModel.name.label("app_name"),
                GeoLocMstModel.name.label("geo_name"),
            )
            .outerjoin(ServicesMstModel, ServicesMstModel.code == KongRouteGroupModel.services_mst_code)
            .outerjoin(ApplicationsMstModel, ApplicationsMstModel.code == ServicesMstModel.applications_mst_code)
            .outerjoin(GeoLocMstModel, GeoLocMstModel.code == KongRouteGroupModel.geo_loc_mst_code)
            .where(KongRouteGroupModel.code.in_(kong_group_codes))
        )
        for row in kong_group_rows:
            tx_app_map[row.code] = {
                "application_code": row.applications_mst_code,
                "application_name": row.app_name,
                "environment": row.environments_enum.value if row.environments_enum else None,
                "geo_code": row.geo_loc_mst_code,
                "geo_name": row.geo_name,
                "vendor": None,
                "name": row.resource_name,
            }

    # ── Build items ───────────────────────────────────────────────────────────
    items: list[DeploymentHistoryItem] = []
    for workflow_id in page_vids:  # preserve the caller's ordering
        g = groups.get(workflow_id)
        if not g:
            continue
        runs: list[PipelineRunTrackModel] = g["runs"]

        # Resolve application + environment + geo + vendor for display from the
        # first resource that has it (all filtering already happened in Phase 1).
        wf_app_code = wf_app_name = wf_env = None
        wf_geo_code = wf_geo_name = wf_vendor = None
        for (tcode, _tname) in g["tx"]:
            meta = tx_app_map.get(tcode)
            if meta:
                wf_app_code = meta["application_code"]
                wf_app_name = meta["application_name"]
                wf_env = meta["environment"]
                wf_geo_code = meta.get("geo_code")
                wf_geo_name = meta.get("geo_name")
                wf_vendor = meta.get("vendor")
                break

        agg_status = _aggregate_status(runs)
        pr_number, pr_url = _extract_pr(runs)

        # First queue item whose user resolves; env-only runs (no queue rows)
        # fall back to the service's update_variables row resolved above.
        wf_user: Optional[UserMstModel] = None
        for r in g["resources"]:
            q = queue_map.get(r["queue_code"])
            if q and q.user_code and q.user_code in user_map:
                wf_user = user_map[q.user_code]
                break
        if wf_user is None:
            for (tcode, _tname) in g["tx"]:
                ucode = variables_user_by_tx.get(tcode)
                if ucode and ucode in user_map:
                    wf_user = user_map[ucode]
                    break

        started_at = min(r.created_at for r in runs)
        terminal = agg_status in ("COMPLETED", "FAILED", "TIMED_OUT")
        completed_at = None
        duration = None
        if terminal:
            ends = [r.updated_at for r in runs if r.updated_at]
            if ends:
                completed_at = max(ends)
                duration = int((completed_at - started_at).total_seconds())

        # Resources — dedupe by queue_code, preserving order.
        resources: list[DeploymentResource] = []
        seen_codes: set[str] = set()
        for r in g["resources"]:
            qcode = r["queue_code"]
            if not qcode or qcode in seen_codes:
                continue
            seen_codes.add(qcode)
            q = queue_map.get(qcode)
            # A multi-deploy batch anchors its ONE run-track row under the
            # service's pipeline, so kong items riding it would inherit
            # SERVICE_CONFIG from the run — take the item's real type from the
            # queue row instead. transaction_code stays the run's resource:
            # it's the canvas deep-link anchor, and kong groups have no node.
            q_table = q.table_name.value if q and q.table_name else None
            # display_name fallback chain: stored name → the item's resolved
            # resource name (older rows have no stored name) → raw queue code.
            q_meta = (
                tx_app_map.get(q.transaction_code)
                if q and q.transaction_code else None
            ) or tx_app_map.get(r["transaction_code"]) or {}
            resources.append(DeploymentResource(
                queue_id=q.id if q else 0,
                queue_code=qcode,
                display_name=(q.display_name or q_meta.get("name") or q.code) if q else qcode,
                queue_status=(q.status.value if q and q.status else "unknown"),
                transaction_code=r["transaction_code"],
                table_name=q_table or r["table_name"],
                resource_type=q_meta.get("resource_type"),
                case_ref_code=q.case_ref_code if q else None,
            ))

        # No queue item: an env-only (variables) run, or a deploy that failed
        # before queueing anything. Either way the row would render blank, so
        # surface the resource the run targeted. Label it from what the run
        # actually did (its stages) rather than assuming.
        if not resources:
            action = "update variables:" if _is_variables_only(runs) else ""
            for (tcode, tname) in g["tx"]:
                meta = tx_app_map.get(tcode) or {}
                resources.append(DeploymentResource(
                    queue_id=0,
                    queue_code=tcode,
                    display_name=f"{action}{meta.get('name') or tcode}",
                    queue_status="unknown",
                    transaction_code=tcode,
                    table_name=tname,
                    resource_type=meta.get("resource_type"),
                ))

        items.append(DeploymentHistoryItem(
            workflow_id=workflow_id,
            status=agg_status,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration,
            user_code=wf_user.code if wf_user else None,
            user_name=f"{wf_user.first_name} {wf_user.last_name}".strip() if wf_user else None,
            user_email=wf_user.email_id if wf_user else None,
            pr_number=pr_number,
            pr_url=pr_url,
            application_code=wf_app_code,
            application_name=wf_app_name,
            environment=wf_env,
            geo_code=wf_geo_code,
            geo_name=wf_geo_name,
            vendor=wf_vendor,
            resources=resources,
        ))

    return items, groups


@router.get("", response_model=DeploymentHistoryResponse)
async def list_deployment_history(
    status: Optional[str] = Query(None, description="RUNNING | COMPLETED | FAILED | TIMED_OUT"),
    user_code: Optional[str] = Query(None, description="Only deployments attributed to this user (via their queue items)"),
    application_code: Optional[str] = Query(None, description="Filter by application/product code"),
    environment: Optional[str] = Query(None, description="Filter by environment (e.g. stage, prod)"),
    geo_code: Optional[str] = Query(None, description="Filter by geo location code"),
    vendor: Optional[str] = Query(None, description="Filter by cloud vendor (aws | gcp | azure | on_prem)"),
    search: Optional[str] = Query(None, description="Free-text search over resource/service names"),
    date_from: Optional[datetime] = Query(None, description="Only deployments started at/after this time (ISO 8601)"),
    date_to: Optional[datetime] = Query(None, description="Only deployments started at/before this time (ISO 8601)"),
    deploy_type: Optional[str] = Query(None, description="gateway | settings | variables | infra"),
    sort_by: Optional[str] = Query(None, description="started_at | duration | status | environment | service"),
    sort_dir: Optional[str] = Query(None, description="asc | desc (default desc)"),
    limit: int = Query(20, ge=1, le=100, description="Page size"),
    page: int = Query(1, ge=1, description="1-based page number"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    _, tenant = current_user_tenant
    vendor_code = f"pv-temporal-{tenant.code}"
    search_lc = search.strip().lower() if search and search.strip() else None

    # ── Phase 1: DB-level pagination ──────────────────────────────────────────
    # A "deployment" is the set of pipeline_run_track rows sharing one
    # vendor_deployment_id. This grouped query resolves each row's metadata via
    # the polymorphic resource tables (service_configs / infrastructure_mst /
    # kong_route_configs), aggregates per deployment, applies every filter, and
    # returns just the page of vendor_deployment_ids + the real total — so the
    # OFFSET/LIMIT happens in Postgres, not in Python.
    params = {
        "tenant": tenant.code,
        "vendor_code": vendor_code,
        "app": application_code,
        "env": environment,
        "geo": geo_code,
        "vendorf": vendor,
        "status": status,
    }

    # Optional date window on the run rows. Applied inside `resolved`, so a
    # deployment shows up as long as any of its runs started in the window.
    date_where = ""
    if date_from is not None:
        params["date_from"] = date_from
        date_where += " AND rt.created_at >= :date_from"
    if date_to is not None:
        params["date_to"] = date_to
        date_where += " AND rt.created_at <= :date_to"

    # Optional deploy-kind filter (gateway / settings / variables / infra).
    # An unknown value filters nothing rather than erroring — the FE only
    # sends the four known keys.
    type_select = type_agg = type_where = ""
    if deploy_type and deploy_type in DEPLOY_TYPE_SQL:
        type_select = f", {DEPLOY_TYPE_SQL[deploy_type]} AS type_match"
        type_agg = ", bool_or(r.type_match) AS type_match"
        type_where = " AND g.type_match"

    # Optional user filter — a deployment matches when ANY of its queue items
    # was created by that user (attribution display uses the first such item;
    # for filtering, "touched by" is the honest semantic). Conditional
    # fragments so the lateral join only runs when the filter is active.
    user_select = user_agg = user_where = ""
    if user_code:
        params["user_code_f"] = user_code
        user_select = (
            f", EXISTS (SELECT 1 FROM jsonb_array_elements_text({_TQ_CODES_ARRAY}) uc(code)"
            " JOIN transaction_queue utq ON utq.code = uc.code"
            " WHERE utq.user_code = :user_code_f) AS user_match"
        )
        user_agg = ", bool_or(r.user_match) AS user_match"
        user_where = " AND g.user_match"

    search_agg = search_join = search_where = ""
    if search_lc:
        params["search_like"] = f"%{search_lc}%"
        # Match queue display names AND the resolved resource name, so
        # queue-less runs (env-only variable deploys) are searchable too.
        search_agg = (
            ", bool_or(tq.display_name ILIKE :search_like"
            " OR r.resource_name ILIKE :search_like) AS name_match"
        )
        search_join = (
            " LEFT JOIN LATERAL jsonb_array_elements_text(COALESCE(r.tq_codes, '[]'::jsonb)) qc(code) ON true"
            " LEFT JOIN transaction_queue tq ON tq.code = qc.code"
        )
        search_where = " AND g.name_match"

    grouped_cte = f"""
        WITH resolved AS (
            SELECT
                rt.vendor_deployment_id AS vid,
                rt.created_at AS created_at,
                rt.updated_at AS updated_at,
                rt.status::text AS run_status,
                COALESCE(sm.applications_mst_code, im.applications_mst_code) AS app_code,
                COALESCE(sc.environment::text, im.environments_enum::text, kr.environments_enum::text, kg.environments_enum::text) AS env,
                COALESCE(sc.geo_loc_mst_code, im.geo_loc_mst_code, kr.geo_loc_mst_code, kg.geo_loc_mst_code) AS geo_code,
                COALESCE(sc.infra_vendor_enum::text, iva.infra_vendor_enum::text) AS vendor,
                COALESCE(sm.name, im.name, kr.api_name, kg.api_name) AS resource_name,
                {_TQ_CODES_ARRAY} AS tq_codes,
                (sc.code IS NOT NULL OR im.code IS NOT NULL OR kr.code IS NOT NULL OR kg.code IS NOT NULL) AS has_resource
                {type_select}
                {user_select}
            FROM pipeline_run_track rt
            JOIN pipeline_mst p ON p.code = rt.pipeline_mst_code
            LEFT JOIN service_configs sc     ON p.table_name::text = 'SERVICE_CONFIG' AND sc.code = p.transaction_code
            LEFT JOIN infrastructure_mst im  ON p.table_name::text = 'INFRASTRUCTURE' AND im.code = p.transaction_code
            LEFT JOIN kong_route_configs kr  ON p.table_name::text = 'KONG_ROUTE'     AND kr.code = p.transaction_code
            -- KONG_ROUTE covers two pointer shapes. The per-route flow points at
            -- kong_route_configs; the Gateway-tab flow points at a route GROUP. Both
            -- joins are kept because both exist, and only one can match a given row
            -- (KRC_ vs KRG_ prefixes). Without the group join a gateway deploy
            -- resolved to NULL app/env/region and the tab's application filter
            -- silently dropped it — the run was recorded but invisible.
            LEFT JOIN kong_route_groups  kg  ON p.table_name::text = 'KONG_ROUTE'     AND kg.code = p.transaction_code
            LEFT JOIN services_mst sm        ON sm.code = COALESCE(sc.services_mst_code, kr.services_mst_code, kg.services_mst_code)
            LEFT JOIN infra_vendor_accounts_mst iva ON iva.code = im.infra_vendor_accounts_mst_code
            WHERE p.tenant_code = :tenant
              AND p.pipeline_vendor_mst_code = :vendor_code
              AND rt.vendor_deployment_id IS NOT NULL
              AND rt.is_deleted = false{date_where}
        ),
        grouped AS (
            SELECT
                r.vid AS vid,
                MAX(r.created_at) AS last_created,
                EXTRACT(EPOCH FROM (MAX(r.updated_at) - MIN(r.created_at))) AS duration_secs,
                MAX(r.env) AS env_val,
                MAX(r.resource_name) AS service_val,
                CASE
                    WHEN bool_or(r.run_status IN ('FAILED','CANCELLED')) THEN 'FAILED'
                    WHEN bool_or(r.run_status = 'TIMEOUT') THEN 'TIMED_OUT'
                    WHEN bool_or(r.run_status IN ('RUNNING','PENDING')) THEN 'RUNNING'
                    ELSE 'COMPLETED'
                END AS agg_status,
                bool_or(CAST(:app AS text) IS NULL OR r.app_code = :app) AS app_match,
                bool_or(CAST(:env AS text) IS NULL OR r.env = :env) AS env_match,
                bool_or(CAST(:geo AS text) IS NULL OR r.geo_code = :geo) AS geo_match,
                bool_or(CAST(:vendorf AS text) IS NULL OR r.vendor = :vendorf) AS vendor_match,
                bool_or(jsonb_array_length(COALESCE(r.tq_codes, '[]'::jsonb)) > 0) AS has_queue,
                bool_or(r.has_resource) AS has_resource
                {type_agg}
                {user_agg}
                {search_agg}
            FROM resolved r{search_join}
            GROUP BY r.vid
        )
    """

    filter_where = (
        " WHERE g.app_match AND g.env_match AND g.geo_match AND g.vendor_match"
        " AND (g.has_queue OR g.has_resource)"
        " AND (CAST(:status AS text) IS NULL OR g.agg_status = :status)"
        f"{type_where}{user_where}{search_where}"
    )

    # Server-side ordering (whitelisted) so page boundaries stay meaningful.
    sort_expr = _SORT_EXPRS.get(sort_by or "started_at", _SORT_EXPRS["started_at"])
    sort_direction = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"
    order_by = f" ORDER BY {sort_expr} {sort_direction} NULLS LAST, g.vid DESC"

    # COUNT(*) OVER () rides along on every page row, so the grouped CTE — the
    # expensive scan — runs once per request instead of once for the count and
    # again for the page.
    page_params = {**params, "limit": limit, "offset": (page - 1) * limit}
    vid_rows = (await db.execute(
        text(
            f"{grouped_cte} SELECT g.vid, COUNT(*) OVER () AS total"
            f" FROM grouped g{filter_where}"
            f"{order_by} LIMIT :limit OFFSET :offset"
        ),
        page_params,
    )).all()
    if not vid_rows:
        # Empty page 1 means no matches at all. A later page past the end loses
        # the window total, so only then pay for a separate count to keep
        # `total` truthful for the FE's pager.
        if page == 1:
            return DeploymentHistoryResponse(items=[], total=0)
        total = (await db.execute(
            text(f"{grouped_cte} SELECT COUNT(*) AS total FROM grouped g{filter_where}"), params
        )).scalar() or 0
        return DeploymentHistoryResponse(items=[], total=total)
    total = vid_rows[0].total
    page_vids = [row.vid for row in vid_rows]

    # ── Phase 2: fetch + enrich only this page's deployments ──────────────────
    items, _groups = await _build_deployment_items(db, page_vids)
    return DeploymentHistoryResponse(items=items, total=total)


@router.get("/{workflow_id}", response_model=DeploymentHistoryDetail)
async def get_deployment_detail(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """One deployment with its merged pipeline stages. Fetched lazily by the
    Admin Dashboard's detail modal (and polled while the deployment is
    RUNNING), so stage payloads never ride on list pages."""
    _, tenant = current_user_tenant
    # Tenant scoping first — a foreign workflow_id must 404, not leak.
    owned = (await db.execute(
        select(PipelineRunTrackModel.id)
        .join(PipelineMstModel, PipelineMstModel.code == PipelineRunTrackModel.pipeline_mst_code)
        .where(
            PipelineRunTrackModel.vendor_deployment_id == workflow_id,
            PipelineRunTrackModel.is_deleted == False,  # noqa: E712
            PipelineMstModel.tenant_code == tenant.code,
            PipelineMstModel.pipeline_vendor_mst_code == f"pv-temporal-{tenant.code}",
        )
        .limit(1)
    )).first()
    if not owned:
        raise HTTPException(status_code=404, detail="Deployment not found")

    items, groups = await _build_deployment_items(db, [workflow_id])
    if not items:
        raise HTTPException(status_code=404, detail="Deployment not found")
    stages = _collect_stages(groups.get(workflow_id, {}).get("runs", []))
    return DeploymentHistoryDetail(**items[0].model_dump(), stages=stages)
