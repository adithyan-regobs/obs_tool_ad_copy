"""
Temporal dashboard service — everything the /temporal/dashboard routes need,
read straight from Temporal via the shared client. Deliberately separate from
temporal_admin.py (the browser-friendly GET tools) so neither can break the other.

Stage ("infra: plan pr" …) is a devlift concept Temporal does not know; it is
read from pipeline_run_track.build_stages, keyed by vendor_deployment_id
(= the orchestrator id for canvas deploys, the workflow id for standalone
ones). Follow-up: upsert a DeployStage search attribute from the workflow and
drop the DB join.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from google.protobuf.json_format import MessageToDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.api.enums.v1 import EventType, PendingActivityState, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import WorkflowExecution, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from app.core.config import settings
from app.db.models.pipeline_run_track_model import PipelineRunTrackModel
from app.db.models.transaction_queue_model import TransactionQueueStatusEnum
from app.schemas.deployment_history_schemas import DeploymentStage
from app.schemas.temporal_dashboard_schemas import (
    TemporalAttentionItem,
    TemporalHistoryEvent,
    TemporalLockHolder,
    TemporalPendingActivity,
    TemporalStats,
    TemporalWorkerInfo,
    TemporalWorkflowDetail,
    TemporalWorkflowItem,
)
from app.temporal.client import get_temporal_client

logger = logging.getLogger(__name__)

# The five classes worker.py registers.
WORKFLOW_TYPES = [
    "TenantCoordinatorWorkflow",
    "DeploymentOrchestratorWorkflow",
    "DeploymentWorkflow",
    "ProductionDeploymentWorkflow",
    "VariableDeployWorkflow",
]

# Temporal spells it CANCELED; the UI (and the deploy tracker) use CANCELLED.
_STATUS = {
    WorkflowExecutionStatus.RUNNING: "RUNNING",
    WorkflowExecutionStatus.COMPLETED: "COMPLETED",
    WorkflowExecutionStatus.FAILED: "FAILED",
    WorkflowExecutionStatus.CANCELED: "CANCELLED",
    WorkflowExecutionStatus.TERMINATED: "TERMINATED",
    WorkflowExecutionStatus.CONTINUED_AS_NEW: "CONTINUED_AS_NEW",
    WorkflowExecutionStatus.TIMED_OUT: "TIMED_OUT",
}

_COORDINATOR_TYPE = "TenantCoordinatorWorkflow"

_ID_TENANT = re.compile(r"^(?:coordinator|multi-deploy|deploy|vars-multi-deploy)-([a-z0-9]+)-?")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _search_attrs(wf: WorkflowExecution) -> Dict[str, Any]:
    """typed_search_attributes → {name: value}; single-valued keys unwrapped."""
    out: Dict[str, Any] = {}
    try:
        for pair in wf.typed_search_attributes:
            out[pair.key.name] = pair.value
    except Exception:  # older payloads / unknown types — fall back to the untyped view
        try:
            for k, v in (wf.search_attributes or {}).items():
                out[k] = v[0] if isinstance(v, list) and len(v) == 1 else v
        except Exception:
            pass
    return out


def _tenant_from_id(workflow_id: str) -> Optional[str]:
    m = _ID_TENANT.match(workflow_id)
    return m.group(1) if m else None


def _environment(workflow_type: str) -> str:
    return "prod" if workflow_type == "ProductionDeploymentWorkflow" else "stage"


# ─── Stage lookup (DB) ───────────────────────────────────────────────────────

async def _stages_by_track_id(db: AsyncSession, track_ids: Iterable[str]) -> Dict[str, tuple[str, str]]:
    """{vendor_deployment_id: (latest stage name, its started_at)} in one query.
    Children of a multi-deploy share the parent's row, so callers pass the
    parent id for children."""
    ids = sorted({i for i in track_ids if i})
    if not ids:
        return {}
    stmt = select(PipelineRunTrackModel.vendor_deployment_id, PipelineRunTrackModel.build_stages).where(
        PipelineRunTrackModel.vendor_deployment_id.in_(ids),
        PipelineRunTrackModel.is_deleted == False,  # noqa: E712
    )
    rows = (await db.execute(stmt)).all()
    latest: Dict[str, tuple[str, str]] = {}
    for vid, stages in rows:
        for st in stages or []:
            name, started = st.get("name"), st.get("started_at")
            if not name or not started:
                continue
            if vid not in latest or started > latest[vid][1]:
                latest[vid] = (name, started)
    return latest


async def _stages_for(db: AsyncSession, track_id: str) -> List[DeploymentStage]:
    """All stage entries for one track id, deduped on (name, started_at) the
    way deployment_history._collect_stages does — the writers append the same
    entry to every row sharing the id."""
    stmt = select(PipelineRunTrackModel.build_stages).where(
        PipelineRunTrackModel.vendor_deployment_id == track_id,
        PipelineRunTrackModel.is_deleted == False,  # noqa: E712
    )
    seen: set[tuple[str, str]] = set()
    out: List[DeploymentStage] = []
    for (stages,) in (await db.execute(stmt)).all():
        for st in stages or []:
            name, started = st.get("name"), st.get("started_at")
            if not name or not started or (name, started) in seen:
                continue
            seen.add((name, started))
            out.append(DeploymentStage(name=name, status=st.get("status") or "completed", started_at=started, ended_at=st.get("ended_at"), error=st.get("error")))
    out.sort(key=lambda s: s.started_at)
    return out


# ─── List ────────────────────────────────────────────────────────────────────

def _item_from(wf: WorkflowExecution, stage: Optional[tuple[str, str]]) -> TemporalWorkflowItem:
    sa = _search_attrs(wf)
    tenant = sa.get("DeployTenantCode") or _tenant_from_id(wf.id)
    start = wf.start_time
    end = wf.close_time or datetime.now(timezone.utc)
    pr = sa.get("DeployPrNumber")
    return TemporalWorkflowItem(
        workflow_id=wf.id,
        run_id=wf.run_id,
        workflow_type=wf.workflow_type,
        status=_STATUS.get(wf.status, "RUNNING") if wf.status else "RUNNING",
        task_queue=wf.task_queue,
        tenant_code=tenant,
        user_code=sa.get("DeployUserCode"),
        environment=_environment(wf.workflow_type),
        stage=stage[0] if stage else None,
        stage_since=stage[1] if stage else None,
        start_time=_iso(start) or "",
        close_time=_iso(wf.close_time),
        execution_time_seconds=max(1, int((end - start).total_seconds())) if start else 1,
        history_length=wf.history_length or 0,
        parent_workflow_id=wf.parent_id,
        parent_run_id=wf.parent_run_id,
        pr_number=int(pr) if isinstance(pr, (int, float)) else None,
        queue_codes=str(sa["DeployQueueCodes"]) if sa.get("DeployQueueCodes") else None,
    )


async def list_workflows(db: AsyncSession, days: int = 7, limit: int = 2000) -> List[TemporalWorkflowItem]:
    """Every run on our task queue started in the window, newest first.
    Coordinators start at app boot (older than any window) so they are
    fetched separately and always included."""
    client = await get_temporal_client()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs: List[WorkflowExecution] = []
    async for wf in client.list_workflows(query=f"StartTime > '{since}'", limit=limit):
        runs.append(wf)
    seen = {(r.id, r.run_id) for r in runs}
    async for wf in client.list_workflows(query="WorkflowType='TenantCoordinatorWorkflow' AND ExecutionStatus='Running'"):
        if (wf.id, wf.run_id) not in seen:
            runs.append(wf)

    # Stage rows are keyed by the orchestrator id for children.
    track_ids = [wf.parent_id or wf.id for wf in runs]
    stages = await _stages_by_track_id(db, track_ids)
    items = [_item_from(wf, stages.get(wf.parent_id or wf.id)) for wf in runs]
    items.sort(key=lambda i: i.start_time, reverse=True)
    return items


# ─── Detail ──────────────────────────────────────────────────────────────────

_CATEGORY_RULES = [
    ("Activity", "activity"),
    ("Timer", "timer"),
    ("Signal", "signal"),
    ("ChildWorkflow", "child"),
    ("StartChildWorkflow", "child"),
    ("Marker", "marker"),
    ("WorkflowTask", "task"),
]
_FAILURE_EVENTS = {
    "WorkflowExecutionFailed", "WorkflowExecutionTimedOut", "WorkflowExecutionTerminated",
    "ActivityTaskFailed", "ActivityTaskTimedOut", "ChildWorkflowExecutionFailed",
    "ChildWorkflowExecutionTimedOut", "WorkflowTaskFailed", "WorkflowTaskTimedOut",
}


def _event_type_name(event_type: int) -> str:
    # EVENT_TYPE_ACTIVITY_TASK_SCHEDULED → ActivityTaskScheduled
    raw = EventType.Name(event_type).removeprefix("EVENT_TYPE_")
    return "".join(p.capitalize() for p in raw.split("_"))


def _category(name: str) -> str:
    for prefix, cat in _CATEGORY_RULES:
        if name.startswith(prefix):
            return cat
    return "workflow"


def _decode_payload_dicts(obj: Any) -> Any:
    """MessageToDict leaves payloads as base64 blobs. Decode json/plain ones
    in place so the UI shows real values; leave anything else as-is."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"payloads"} and isinstance(obj["payloads"], list):
            out = []
            for p in obj["payloads"]:
                try:
                    enc = base64.b64decode((p.get("metadata") or {}).get("encoding", "")).decode()
                    data = base64.b64decode(p.get("data", ""))
                    out.append(json.loads(data) if enc == "json/plain" else (None if enc == "binary/null" else data.decode(errors="replace")))
                except Exception:
                    out.append(p)
            return out[0] if len(out) == 1 else out
        return {k: _decode_payload_dicts(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode_payload_dicts(v) for v in obj]
    return obj


def _summary(name: str, attrs: Dict[str, Any]) -> Optional[str]:
    if name.startswith("Activity"):
        t = attrs.get("activity_type")
        return t.get("name") if isinstance(t, dict) else None
    if name == "WorkflowExecutionSignaled":
        return attrs.get("signal_name")
    if name.startswith("StartChildWorkflow") or name.startswith("ChildWorkflow"):
        return attrs.get("workflow_id")
    if name == "MarkerRecorded":
        return attrs.get("marker_name")
    if name.startswith("Timer"):
        return f"timer {attrs.get('timer_id')}"
    if name == "WorkflowExecutionTerminated":
        return attrs.get("reason")
    return None


def _pending(pa) -> TemporalPendingActivity:
    def ts(field: str) -> Optional[str]:
        return pa.__getattribute__(field).ToDatetime(tzinfo=timezone.utc).isoformat() if pa.HasField(field) else None
    return TemporalPendingActivity(
        activity_id=pa.activity_id,
        activity_type=pa.activity_type.name,
        state=PendingActivityState.Name(pa.state).removeprefix("PENDING_ACTIVITY_STATE_"),
        attempt=pa.attempt,
        maximum_attempts=pa.maximum_attempts,
        scheduled_time=ts("scheduled_time"),
        last_started_time=ts("last_started_time"),
        last_heartbeat_time=ts("last_heartbeat_time"),
        last_failure=pa.last_failure.message if pa.HasField("last_failure") else None,
        last_worker_identity=pa.last_worker_identity or None,
    )


async def get_workflow(db: AsyncSession, workflow_id: str, run_id: Optional[str]) -> TemporalWorkflowDetail:
    client = await get_temporal_client()
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    desc = await handle.describe()
    history = await handle.fetch_history()

    stage_key = desc.parent_id or desc.id
    stage = (await _stages_by_track_id(db, [stage_key])).get(stage_key)
    stages = await _stages_for(db, stage_key)
    base = _item_from(desc, stage)

    events: List[TemporalHistoryEvent] = []
    input_value: Any = None
    result_value: Any = None
    failure: Optional[str] = None
    children: List[str] = []
    for ev in history.events:
        name = _event_type_name(ev.event_type)
        which = ev.WhichOneof("attributes")
        attrs = _decode_payload_dicts(MessageToDict(getattr(ev, which), preserving_proto_field_name=True)) if which else {}
        if name == "WorkflowExecutionStarted":
            input_value = attrs.get("input")
        elif name == "WorkflowExecutionCompleted":
            result_value = attrs.get("result")
        elif name in ("WorkflowExecutionFailed", "WorkflowExecutionTimedOut", "WorkflowExecutionTerminated"):
            f = attrs.get("failure") or {}
            failure = f.get("message") if isinstance(f, dict) else None
            failure = failure or attrs.get("reason") or name
        elif name == "StartChildWorkflowExecutionInitiated" and attrs.get("workflow_id"):
            children.append(attrs["workflow_id"])
        events.append(TemporalHistoryEvent(
            event_id=ev.event_id,
            event_type=name,
            category=_category(name),
            timestamp=ev.event_time.ToDatetime(tzinfo=timezone.utc).isoformat(),
            summary=_summary(name, attrs),
            attributes=attrs,
            is_failure=name in _FAILURE_EVENTS,
        ))

    prior: List[str] = []
    async for other in client.list_workflows(query=f"WorkflowId='{workflow_id}'"):
        if other.run_id != desc.run_id:
            prior.append(other.run_id)

    return TemporalWorkflowDetail(
        **base.model_dump(),
        input=input_value,
        result=result_value,
        failure=failure,
        search_attributes=_search_attrs(desc),
        pending_activities=[_pending(pa) for pa in desc.raw_description.pending_activities],
        history=events,
        child_workflow_ids=children,
        prior_run_ids=prior,
        stages=stages,
    )


# ─── Actions ─────────────────────────────────────────────────────────────────

async def query_state(workflow_id: str, run_id: Optional[str]) -> Any:
    client = await get_temporal_client()
    return await client.get_workflow_handle(workflow_id, run_id=run_id).query("get_state")


async def terminate(workflow_id: str, run_id: Optional[str], reason: Optional[str], actor: str) -> str:
    """Terminate a run, then hand its coordinator locks back. Returns a message
    describing what actually happened, for the caller to surface.

    Two things this must not get wrong.

    Coordinators are refused. One holds every lock and every hold-queue entry
    for its tenant and is restarted only by the API on boot, so terminating one
    wedges every deploy for that tenant with no way back from this dashboard.
    `retry` already refuses them and the older admin sweep skips them.

    Termination runs no workflow code, so the run's own release never fires and
    the coordinator keeps the dirs assigned to a workflow that is now dead.
    Left alone that is precisely the stale lock this dashboard alerts on, and a
    later retry under the same id would queue behind its own corpse. So we send
    the release_hold + release_locks pair the coordinator documents as the
    race-free cleanup -- the same pair the lock-wait timeout path sends, and the
    same signal the older release-lock route uses.

    That cleanup is best-effort. The run is already gone by then, so a failure
    is reported in the message rather than raised, and the release-lock route
    stays the manual fallback.
    """
    client = await get_temporal_client()
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)

    desc = await handle.describe()
    if desc.workflow_type == _COORDINATOR_TYPE:
        raise ValueError(
            "Coordinators hold every lock for their tenant and are restarted only by "
            "the API on boot - terminating one would wedge every deploy for that tenant"
        )

    await handle.terminate(reason or f"terminated from devlift dashboard by {actor}")

    tenant_code = _search_attrs(desc).get("DeployTenantCode") or _tenant_from_id(workflow_id)
    if not tenant_code:
        logger.warning("terminated %s but could not resolve its tenant; locks not released", workflow_id)
        return "terminated; tenant could not be resolved, release any held locks by hand"

    try:
        coordinator = client.get_workflow_handle(f"coordinator-{tenant_code}")
        # Both, in this order: whichever state the run was in (still queued, or
        # holding) one signal cleans it up and the other is a no-op.
        await coordinator.signal("release_hold", workflow_id)
        await coordinator.signal("release_locks", workflow_id)
    except Exception:  # noqa: BLE001 - the run is already terminated; cleanup must not fail the action
        logger.warning(
            "terminated %s but failed to release its locks on coordinator-%s",
            workflow_id, tenant_code, exc_info=True,
        )
        return "terminated; lock release failed, use the coordinator release-lock route"
    return "terminated and locks released"


async def _readmit_queue_items(db: AsyncSession, args: List[Any]) -> tuple[int, int]:
    """Put a batch's queue items back where prep can see them. Returns
    (re-admitted, still-eligible-already) counts.

    `prepare_multiple_deploy` admits only APPROVED items, and the first run's
    prep already moved them to STARTING_DEPLOYMENT. Without this, a retry starts
    a run that dies immediately with "No pending items to deploy" and writes a
    second confusing timeline against the same id.

    ONLY items still sitting at STARTING_DEPLOYMENT are re-admitted, and that
    restraint is the whole point. That status means prep claimed the item and
    nothing downstream ever touched it: the deploy path moves items on to
    PR_RAISED / DEPLOYING / DEPLOYED / FAILED as it goes, and the early failures
    this retry exists for (lock timeout, lock abort, prep error) return without
    touching them at all. So a half-finished batch keeps its finished work
    finished, and only the part that never ran is run again.
    """
    params = args[0] if args and isinstance(args[0], dict) else {}
    infra = params.get("infra") or {}
    item_ids = list(infra.get("item_ids") or [])
    if not item_ids:
        return 0, 0

    from app.repository.transaction_queue_repository import TransactionQueueRepository

    repo = TransactionQueueRepository(db)
    readmitted = 0
    already = 0
    for item_id in item_ids:
        item = await repo.get_by_id(item_id)
        if item is None:
            continue
        if item.status == TransactionQueueStatusEnum.APPROVED:
            already += 1
        elif item.status == TransactionQueueStatusEnum.STARTING_DEPLOYMENT:
            await repo.update_status(item.id, TransactionQueueStatusEnum.APPROVED.value)
            readmitted += 1
    if readmitted:
        logger.info("retry: re-admitted %d queue item(s) left at STARTING_DEPLOYMENT", readmitted)
    return readmitted, already


async def retry(db: AsyncSession, workflow_id: str, run_id: Optional[str]) -> str:
    """New run, same id, same type and input as the given (closed) run.
    Returns the new run id.

    Two things make this harder than it looks.

    DeploymentOrchestratorWorkflow reports failure by RETURNING
    {"status": "FAILED"}, so Temporal records the run as COMPLETED and
    ALLOW_DUPLICATE_FAILED_ONLY refuses to reuse the id — the runs an admin most
    wants to retry were exactly the ones it turned away, with an opaque "already
    started" error. So we read the run's own result: a returned failure earns
    ALLOW_DUPLICATE, and a genuine success is refused here with a message that
    says so rather than by a confusing server error.

    And the first run's prep consumed the batch's queue items, so they have to be
    re-admitted or the new run dies on arrival. See _readmit_queue_items for why
    only STARTING_DEPLOYMENT items qualify.
    """
    client = await get_temporal_client()
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    desc = await handle.describe()
    if desc.status == WorkflowExecutionStatus.RUNNING:
        raise ValueError("Workflow is still running — terminate it first")
    if desc.workflow_type == _COORDINATOR_TYPE:
        raise ValueError("Coordinators are restarted by the API on boot, not from here")

    history = await handle.fetch_history()
    started = next((e for e in history.events if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED), None)
    if started is None:
        raise ValueError("Could not read the original input from history")
    args = await client.data_converter.decode(started.workflow_execution_started_event_attributes.input.payloads)

    reuse = WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
    if desc.status == WorkflowExecutionStatus.COMPLETED:
        if _returned_failure(await _decode_result(client, history)):
            reuse = WorkflowIDReusePolicy.ALLOW_DUPLICATE
        else:
            raise ValueError("That run finished successfully — there is nothing to retry")

    readmitted, already = await _readmit_queue_items(db, args)
    params = args[0] if args and isinstance(args[0], dict) else {}
    if (params.get("infra") or {}).get("item_ids") and not readmitted and not already:
        raise ValueError(
            "None of this batch's queue items can be retried — they have already "
            "moved past deployment (deployed, failed or mid-PR). Re-approve them "
            "from the canvas to deploy again."
        )

    if readmitted:
        # Commit BEFORE starting. The new run's prep activity reads these rows
        # from its own session in the worker and it runs immediately, so leaving
        # the flush uncommitted until the request ends races prep to the commit
        # -- and prep loses, seeing STARTING_DEPLOYMENT and bailing out with
        # "No pending items to deploy", the very failure this is here to remove.
        # If the start below then fails, the items are simply left APPROVED,
        # which is their normal pre-deploy state and safe to retry from.
        await db.commit()

    new_handle = await client.start_workflow(
        desc.workflow_type,
        args=args,
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
        id_reuse_policy=reuse,
    )
    return new_handle.result_run_id or ""


async def _decode_result(client: Any, history: Any) -> Any:
    """The value a COMPLETED run returned, or None."""
    completed = next(
        (e for e in history.events if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED),
        None,
    )
    if completed is None:
        return None
    payloads = completed.workflow_execution_completed_event_attributes.result.payloads
    if not payloads:
        return None
    decoded = await client.data_converter.decode(payloads)
    return decoded[0] if decoded else None


def _returned_failure(result: Any) -> bool:
    """A run that closed COMPLETED but whose own result says it failed."""
    return isinstance(result, dict) and str(result.get("status") or "").upper() == "FAILED"


# ─── Stats + worker ──────────────────────────────────────────────────────────

async def stats() -> TemporalStats:
    client = await get_temporal_client()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def count(q: str) -> int:
        return (await client.count_workflows(q)).count

    closed = f"CloseTime > '{since}'"
    return TemporalStats(
        running=await count("ExecutionStatus='Running'"),
        completed_24h=await count(f"ExecutionStatus='Completed' AND {closed}"),
        failed_24h=await count(f"ExecutionStatus='Failed' AND {closed}"),
        timed_out_24h=await count(f"ExecutionStatus='TimedOut' AND {closed}"),
        terminated_24h=await count(f"ExecutionStatus='Terminated' AND {closed}"),
        cancelled_24h=await count(f"ExecutionStatus='Canceled' AND {closed}"),
    )


async def worker_info() -> TemporalWorkerInfo:
    """DescribeTaskQueue for both poller kinds. A poller seen in the last
    minute means the worker is up; worker.py long-polls every few seconds."""
    client = await get_temporal_client()
    ns, q = settings.temporal_namespace, settings.temporal_task_queue

    async def pollers(kind: int):
        resp = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(namespace=ns, task_queue=TaskQueue(name=q), task_queue_type=kind)
        )
        return list(resp.pollers)

    wf_pollers = await pollers(TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW)
    act_pollers = await pollers(TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY)
    all_pollers = wf_pollers + act_pollers
    latest = max((p.last_access_time.ToDatetime(tzinfo=timezone.utc) for p in all_pollers if p.HasField("last_access_time")), default=None)
    alive = latest is not None and (datetime.now(timezone.utc) - latest) < timedelta(seconds=60)
    return TemporalWorkerInfo(
        task_queue=q,
        namespace=ns,
        status="HEALTHY" if alive else "OFFLINE",
        identity=all_pollers[0].identity if all_pollers else None,
        workflow_pollers=len(wf_pollers),
        activity_pollers=len(act_pollers),
        last_access_time=_iso(latest),
        workflow_types=WORKFLOW_TYPES,
    )


# ─── Stuck deploys ───────────────────────────────────────────────────────────

STUCK_STAGE = "waiting in queue"


async def check_stale_locks(tenant_code: str, waiting_workflow_id: str, dirs: Optional[List[str]] = None) -> tuple[List[str], List[TemporalLockHolder]]:
    """Who holds the directories a queued deploy is waiting for, and whether
    each holder is still running. Reads the coordinator's get_state
    (active_locks {dir: holder}, hold_queue [{workflow_id, waiting_for}]) and
    describes each holder.

    Returns (waiting_for, holders). `stale` means the wait will not resolve on
    its own and an admin must act. Two separate cases earn it: a holder in a
    closed state, or EVERY dir free while we are still queued (nothing blocks
    us, so the coordinator's grant scan is wedged).

    One free dir among held ones is NOT stale. The coordinator grants
    all-or-nothing, so a request blocked on one dir claims none of the others
    -- every multi-dir waiter has free dirs while waiting perfectly normally,
    and flagging them paged admins on ordinary production queueing.

    A holder we cannot read is UNKNOWN and explicitly NOT stale. Not knowing is
    not evidence, and treating it as evidence would page admins about a live
    deploy on every network blip. For the same reason an unreadable coordinator
    returns no holders rather than raising: this is telemetry, and a queued
    deploy must never die because the telemetry failed.
    """
    client = await get_temporal_client()
    try:
        state = await client.get_workflow_handle(f"coordinator-{tenant_code}").query("get_state")
    except Exception:  # noqa: BLE001 - a coordinator we cannot read is not evidence of a stale lock
        logger.warning(
            "coordinator-%s get_state failed; reporting no holders (treated as a genuine wait)",
            tenant_code,
            exc_info=True,
        )
        return list(dirs or []), []
    active: Dict[str, str] = dict(state.get("active_locks") or {})
    entry = next((h for h in state.get("hold_queue") or [] if h.get("workflow_id") == waiting_workflow_id), None)
    if dirs is None:
        dirs = list(entry.get("waiting_for") or []) if entry else []

    holders: List[TemporalLockHolder] = []
    status_cache: Dict[str, Optional[str]] = {}
    for d in dirs:
        holder = active.get(d)
        if holder is None:
            # Free -- and on its own that means nothing. The coordinator grants
            # all-or-nothing, so a request blocked on ONE dir claims none of the
            # others; every multi-dir waiter therefore has free dirs while it
            # waits perfectly normally. Judged together after the loop.
            holders.append(TemporalLockHolder(dir=d, stale=False))
            continue
        if holder not in status_cache:
            try:
                desc = await client.get_workflow_handle(holder).describe()
                status_cache[holder] = _STATUS.get(desc.status, "UNKNOWN") if desc.status else "UNKNOWN"
            except RPCError as exc:
                # Only a real "no such workflow" proves the holder is gone. Any
                # other RPC failure (server unreachable, deadline exceeded) means
                # we do not know, and not knowing must never read as stale --
                # otherwise a network blip pages admins about a live deploy.
                status_cache[holder] = "NOT_FOUND" if exc.status == RPCStatusCode.NOT_FOUND else "UNKNOWN"
            except Exception:  # noqa: BLE001 - same reasoning: unknown, not stale
                logger.warning("describe(%s) failed; holder status unknown", holder, exc_info=True)
                status_cache[holder] = "UNKNOWN"
        st = status_cache[holder]
        holders.append(TemporalLockHolder(dir=d, holder_workflow_id=holder, holder_status=st, stale=st not in ("RUNNING", "UNKNOWN")))

    # Free dirs only mean something when EVERY dir we need is free: then nothing
    # is blocking us, the coordinator's grant scan should already have run, and
    # the queue itself is wedged (a lost request, or a scan that never fired).
    # That is a real incident, but a different one from a stale lock -- and it
    # is the only case where "no holder" earns an alert.
    if holders and all(h.holder_workflow_id is None for h in holders):
        logger.warning(
            "[%s] queued on %s but nothing holds any of them (queued=%s) - coordinator-%s may be wedged",
            waiting_workflow_id, dirs, entry is not None, tenant_code,
        )
        for h in holders:
            h.stale = True

    return dirs, holders


async def post_admin_alert(text: str, blocks: Optional[list] = None) -> bool:
    """Post to the admin report channel via the deploy-tracker bot (the same
    token/channel as the Deploy Tracker's Share Report). False when the bot is
    not configured — callers treat that as 'not alerted'."""
    token = (settings.deploy_tracker_slack_bot_token or "").strip()
    channel = (settings.deploy_tracker_slack_channel or "").strip()
    if not token or not channel:
        logger.warning("admin alert skipped: DEPLOY_TRACKER_SLACK_BOT_TOKEN / _CHANNEL not set")
        return False
    from slack_sdk.web.async_client import AsyncWebClient
    try:
        await AsyncWebClient(token=token).chat_postMessage(channel=channel, text=text, blocks=blocks)
    except Exception:  # noqa: BLE001 - a failed post is "not alerted", never a failed deploy
        logger.warning("admin alert failed to post to %s", channel, exc_info=True)
        return False
    return True


def stale_lock_alert(tenant_code: str, waiting_workflow_id: str, user_code: Optional[str], waited_min: int, dirs: List[str], holders: List[TemporalLockHolder], reminder: bool = False) -> tuple[str, list]:
    """Slack text + blocks for a stale-lock alert. Admin-facing: names the
    holder, its status and the exact release call. `reminder` = not the first
    alert for this run; the header says so, the body is the same."""
    stale = [h for h in holders if h.stale]
    dead = [h for h in stale if h.holder_workflow_id]

    if dead:
        # A named holder that is no longer running. Release exactly that id.
        lines = [f"• `{h.dir}` held by `{h.holder_workflow_id}` — *{h.holder_status}*" for h in dead]
        headline = "*Lock holder is no longer running:*"
        release = sorted({f"`/api/v1/temporal/coordinator/coordinator-{tenant_code}/release-lock/{h.holder_workflow_id}`" for h in dead})
        fix = "release the stale lock — " + ", ".join(release)
        subject, kind = "Stale lock", "stale lock"
    else:
        # Nothing holds any of them, yet we are still queued: the coordinator's
        # grant scan never ran for us, or our request was lost. Releasing under
        # our OWN id frees nothing (we hold nothing) and re-runs the scan, so it
        # is the safe nudge. Never suggest /reset here — it wipes every lock for
        # the tenant, including the ones live deploys are relying on.
        lines = [f"• `{h.dir}` — free, nothing holds it" for h in stale]
        headline = "*Nothing holds these directories, yet the deploy is still queued:*"
        fix = (
            "re-run the coordinator's grant scan — "
            f"`/api/v1/temporal/coordinator/coordinator-{tenant_code}/release-lock/{waiting_workflow_id}`"
            " (releases nothing, just rescans)"
        )
        subject, kind = "Deploy stuck in the queue", "queue wedge"

    body = (
        f"*Deploy:* `{waiting_workflow_id}` · tenant *{tenant_code}*" + (f" · by {user_code}" if user_code else "") + "\n"
        f"*Waiting in queue:* {waited_min} min for `{', '.join(dirs)}`\n\n"
        + headline + "\n" + "\n".join(lines) + "\n\n"
        "*Fix:* " + fix +
        "\nThen the queued deploy starts on its own. Admin Dashboard → Temporal shows it under *Stuck deploys*."
    )
    if reminder:
        text = f":lock: Reminder — {kind} still blocking `{waiting_workflow_id}` ({tenant_code}) after {waited_min} min"
        header = f"Reminder: {subject.lower()} is still blocking a deploy"
    else:
        text = f":lock: {subject} blocking `{waiting_workflow_id}` ({tenant_code}) for {waited_min} min"
        header = f"{subject} is blocking a deploy"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    return text, blocks


async def find_attention(db: AsyncSession, days: int = 7) -> List[TemporalAttentionItem]:
    """Running deploys that are genuinely stuck, longest first. Read-only —
    feeds the dashboard strip. The admin Slack alert for stale locks is raised
    by the workflows (report_stale_queue_wait); plan / apply / merge / lock-
    conflict stalls already raise the existing P0 alerts.

    A deploy queued behind a RUNNING holder is normal and is NOT listed.
    Coordinators never count: they run forever by design.

    "Queued" is read from the coordinator's own hold_queue, not from the DB
    stage string. Only DeploymentOrchestratorWorkflow ever writes
    "waiting in queue"; standalone DeploymentWorkflow and
    ProductionDeploymentWorkflow runs write no stage at all until locks are
    granted. Keying on the stage silently skipped every standalone deploy --
    the Slack alert told admins to look for the run under Stuck deploys and it
    was never there. The hold_queue is authoritative and covers every type.
    """
    now = datetime.now(timezone.utc)
    queue_limit = timedelta(minutes=settings.temporal_stuck_alert_after_min)
    progress_limit = timedelta(minutes=settings.temporal_stuck_no_progress_min)

    items = await list_workflows(db, days=days)
    running = [i for i in items if i.status == "RUNNING" and i.workflow_type != "TenantCoordinatorWorkflow"]
    if not running:
        return []

    client = await get_temporal_client()

    # One get_state per tenant, not per item: workflow_id -> the dirs it waits on.
    queued: Dict[str, List[str]] = {}
    for tenant in sorted({i.tenant_code for i in running if i.tenant_code}):
        try:
            state = await client.get_workflow_handle(f"coordinator-{tenant}").query("get_state")
        except Exception as exc:  # noqa: BLE001 - one unreadable coordinator must not blank the strip
            logger.warning("coordinator-%s get_state failed; its queued deploys are not listed: %s", tenant, exc)
            continue
        for hold in state.get("hold_queue") or []:
            wid = hold.get("workflow_id")
            if wid:
                queued[wid] = list(hold.get("waiting_for") or [])

    out: List[TemporalAttentionItem] = []
    for item in running:
        since_dt = datetime.fromisoformat(item.stage_since) if item.stage_since else datetime.fromisoformat(item.start_time)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)

        waiting_dirs = queued.get(item.workflow_id)
        if waiting_dirs is not None or item.stage == STUCK_STAGE:
            # A queued standalone run has no stage, so `since` falls back to its
            # start time -- acquire_locks is one of the first things it does, so
            # that is very close to when the queueing began.
            if now - since_dt <= queue_limit or not item.tenant_code:
                continue
            try:
                dirs, holders = await check_stale_locks(item.tenant_code, item.workflow_id, waiting_dirs or None)
            except Exception as exc:
                logger.warning("stale-lock check failed for %s: %s", item.workflow_id, exc)
                continue
            if any(h.stale for h in holders):
                out.append(TemporalAttentionItem(workflow=item, reason="stale_lock", since=since_dt.isoformat(), waiting_for=dirs, holders=holders))
            continue  # queued behind a live deploy — genuine, not stuck

        try:
            desc = await client.get_workflow_handle(item.workflow_id, run_id=item.run_id).describe()
            retrying = [pa for pa in desc.raw_description.pending_activities if pa.attempt > 1]
        except Exception:
            retrying = []
        if retrying:
            pa = retrying[0]
            since = pa.scheduled_time.ToDatetime(tzinfo=timezone.utc) if pa.HasField("scheduled_time") else since_dt
            out.append(TemporalAttentionItem(workflow=item, reason="activity_retrying", since=since.isoformat()))
        elif item.stage and now - since_dt > progress_limit:
            out.append(TemporalAttentionItem(workflow=item, reason="no_progress", since=since_dt.isoformat()))

    out.sort(key=lambda i: i.since)
    return out
