"""
Temporal admin endpoints — all GET so they can be used directly from a browser.

  GET /temporal/workflows                                          — list all workflows
  GET /temporal/workflows?status=Running                           — filter by status
  GET /temporal/workflows?workflow_type=DeploymentWorkflow
  GET /temporal/workflows/{workflow_id}                            — describe one
  GET /temporal/workflows/{workflow_id}/terminate                  — terminate it
  GET /temporal/coordinator/{coordinator_id}/state                 — locks + queues
  GET /temporal/coordinator/{coordinator_id}/release-lock/{wf_id} — force-release a stuck lock
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from app.temporal.client import get_temporal_client

router = APIRouter()


@router.get("/workflows")
async def list_workflows(
    status: Optional[str] = Query(
        None,
        description="Running | Completed | Failed | TimedOut | Terminated | Canceled",
    ),
    workflow_type: Optional[str] = Query(
        None,
        description="e.g. DeploymentWorkflow or TenantCoordinatorWorkflow",
    ),
):
    """List Temporal workflows. No filters = all workflows."""
    client = await get_temporal_client()

    filters = []
    if status:
        filters.append(f"ExecutionStatus='{status}'")
    if workflow_type:
        filters.append(f"WorkflowType='{workflow_type}'")
    query = " AND ".join(filters)

    workflows = []
    async for wf in client.list_workflows(query=query):
        workflows.append({
            "workflow_id": wf.id,
            "run_id": wf.run_id,
            "workflow_type": wf.workflow_type,
            "status": wf.status.name if wf.status else None,
            "start_time": wf.start_time.isoformat() if wf.start_time else None,
            "close_time": wf.close_time.isoformat() if wf.close_time else None,
        })

    return {"count": len(workflows), "workflows": workflows}


@router.get("/workflows/terminate-all")
async def terminate_all_workflows(
    reason: str = Query(
        "bulk terminate via temporal-admin",
        description="Termination reason recorded on each workflow",
    ),
):
    """Terminate every RUNNING workflow EXCEPT the per-tenant coordinators.

    Cleanup tool for releasing workflow-logic changes: in-flight workflows
    replay against the new code and die with nondeterminism errors (TMPRL1100),
    stuck failing their workflow tasks forever while holding coordinator locks.
    After terminating, wipe the coordinator's lock state with the reset_state
    signal (POST /transaction-queue/temporal-signal/coordinator-{tenant}/reset_state).

    NOTE: registered BEFORE /workflows/{workflow_id} so 'terminate-all' isn't
    captured as a workflow id.
    """
    client = await get_temporal_client()
    terminated = []
    failed = []
    async for wf in client.list_workflows(query='ExecutionStatus="Running"'):
        if wf.workflow_type == "TenantCoordinatorWorkflow":
            continue
        try:
            # Pin the run_id so we never terminate a newer run of a reused id.
            await client.get_workflow_handle(wf.id, run_id=wf.run_id).terminate(reason)
            terminated.append({"workflow_id": wf.id, "workflow_type": wf.workflow_type})
        except Exception as e:
            # Children of just-terminated parents may already be gone
            # (parent close policy) — report, don't abort the sweep.
            failed.append({"workflow_id": wf.id, "error": str(e)})
    return {
        "terminated_count": len(terminated),
        "terminated": terminated,
        "failed": failed,
    }


@router.get("/workflows/{workflow_id}")
async def describe_workflow(workflow_id: str):
    """Describe a specific Temporal workflow."""
    client = await get_temporal_client()
    try:
        handle = client.get_workflow_handle(workflow_id)
        desc = await handle.describe()
        return {
            "workflow_id": desc.id,
            "run_id": desc.run_id,
            "workflow_type": desc.workflow_type,
            "status": desc.status.name if desc.status else None,
            "task_queue": desc.task_queue,
            "start_time": desc.start_time.isoformat() if desc.start_time else None,
            "close_time": desc.close_time.isoformat() if desc.close_time else None,
            "history_length": desc.history_length,
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/workflows/{workflow_id}/terminate")
async def terminate_workflow(
    workflow_id: str,
    reason: str = Query(default="terminated via admin API"),
):
    """Terminate a running Temporal workflow."""
    client = await get_temporal_client()
    try:
        handle = client.get_workflow_handle(workflow_id)
        await handle.terminate(reason=reason)
        return {"terminated": True, "workflow_id": workflow_id, "reason": reason}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workflows/{workflow_id}/history")
async def get_workflow_history(workflow_id: str, run_id: str | None = None):
    """
    Raw event history as JSON — feed it to scripts/replay_coordinator_history.py
    to prove a workflow-code change replays before deploying it.

    Large: a long-lived coordinator runs to several MB. Pin run_id when the
    workflow ID has continued-as-new and you want an older run.
    """
    client = await get_temporal_client()
    try:
        handle = client.get_workflow_handle(workflow_id, run_id=run_id)
        history = await handle.fetch_history()
        return history.to_json_dict()
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/coordinator/{coordinator_id}/state")
async def get_coordinator_state(coordinator_id: str):
    """
    Query coordinator internal state: active_locks, hold_queue, merge_queue.
    coordinator_id: coordinator-aspora | coordinator-vance
    """
    client = await get_temporal_client()
    try:
        handle = client.get_workflow_handle(coordinator_id)
        state = await handle.query("get_state")
        return state
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/coordinator/{coordinator_id}/reset")
async def reset_coordinator_state(coordinator_id: str):
    """
    Wipe the coordinator's ENTIRE state: active_locks, hold_queue, merge_queue.

    Cleanup tool — use after /workflows/terminate-all when in-flight deploys
    were killed (e.g. bricked by a workflow-logic change) and their locks /
    queued requests are stale. Terminate first, then reset, so no dying
    workflow can re-acquire between the two calls.
    coordinator_id: coordinator-aspora | coordinator-vance
    """
    client = await get_temporal_client()
    try:
        handle = client.get_workflow_handle(coordinator_id)
        await handle.signal("reset_state")
        state = await handle.query("get_state")
        return {"reset": True, "coordinator_id": coordinator_id, "state": state}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/coordinator/{coordinator_id}/release-lock/{workflow_id}")
async def release_coordinator_lock(coordinator_id: str, workflow_id: str):
    """
    Force-release a stuck lock held by a terminated/stuck DeploymentWorkflow.
    Use when a workflow was externally terminated and its lock was not cleaned up.
    coordinator_id: coordinator-aspora | coordinator-vance
    workflow_id: the deploy-aspora-xxxxx that holds the lock
    """
    client = await get_temporal_client()
    try:
        handle = client.get_workflow_handle(coordinator_id)
        await handle.signal("release_locks", workflow_id)
        return {"released": True, "coordinator_id": coordinator_id, "workflow_id": workflow_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))