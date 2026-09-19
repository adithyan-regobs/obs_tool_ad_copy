"""
Temporal view of the Admin Dashboard — /temporal/dashboard/*.

Thin routes over app.services.temporal_dashboard_service. Gated like the
Deploy Tracker (org owners + DEPLOY_TRACKER_ADMIN_EMAILS). Separate from
temporal_admin.py, whose browser-friendly GET tools stay untouched.

  GET  /temporal/dashboard/workflows?days=7            — runs in the window (+ live coordinators)
  GET  /temporal/dashboard/workflows/{id}?run_id=      — describe + history + stage + children
  GET  /temporal/dashboard/workflows/{id}/state        — the class's get_state query
  POST /temporal/dashboard/workflows/{id}/terminate    — {run_id?, reason?}
  POST /temporal/dashboard/workflows/{id}/retry        — {run_id?} → new run, same input
  GET  /temporal/dashboard/stats                       — tiles
  GET  /temporal/dashboard/worker                      — poller health for deploy-queue
  GET  /temporal/dashboard/attention                   — stuck deploys (+ whether Slack was told)
"""

import logging
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.api.v1.endpoints.admin_deploy_tracker import require_deploy_tracker_admin
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.core.config import settings
from app.schemas.temporal_dashboard_schemas import (
    TemporalActionResponse,
    TemporalAttentionResponse,
    TemporalRetryRequest,
    TemporalStateResponse,
    TemporalStats,
    TemporalTerminateRequest,
    TemporalWorkerInfo,
    TemporalWorkflowDetail,
    TemporalWorkflowListResponse,
)
from app.services import temporal_dashboard_service as svc

logger = logging.getLogger(__name__)
router = APIRouter()

Admin = Tuple[UserMstModel, TenantsMstModel]


@router.get("/workflows", response_model=TemporalWorkflowListResponse)
async def list_workflows(
    days: int = Query(7, ge=1, le=90, description="Window, in days, by start time"),
    db: AsyncSession = Depends(get_db),
    _: Admin = Depends(require_deploy_tracker_admin),
):
    try:
        items = await svc.list_workflows(db, days=days)
    except Exception as e:
        logger.exception("temporal dashboard: list failed")
        raise HTTPException(status_code=502, detail=f"Temporal unavailable: {e}")
    return TemporalWorkflowListResponse(count=len(items), items=items)


@router.get("/workflows/{workflow_id}", response_model=TemporalWorkflowDetail)
async def get_workflow(
    workflow_id: str,
    run_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: Admin = Depends(require_deploy_tracker_admin),
):
    try:
        return await svc.get_workflow(db, workflow_id, run_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/workflows/{workflow_id}/state", response_model=TemporalStateResponse)
async def get_state(
    workflow_id: str,
    run_id: Optional[str] = Query(None),
    _: Admin = Depends(require_deploy_tracker_admin),
):
    try:
        state = await svc.query_state(workflow_id, run_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return TemporalStateResponse(workflow_id=workflow_id, run_id=run_id or "", state=state)


@router.post("/workflows/{workflow_id}/terminate", response_model=TemporalActionResponse)
async def terminate_workflow(
    workflow_id: str,
    body: TemporalTerminateRequest,
    admin: Admin = Depends(require_deploy_tracker_admin),
):
    user, _ = admin
    try:
        message = await svc.terminate(workflow_id, body.run_id, body.reason, actor=user.email_id or user.code)
    except ValueError as e:
        # Refused on purpose (a coordinator) — 409 like retry, not a generic 400.
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return TemporalActionResponse(ok=True, workflow_id=workflow_id, run_id=body.run_id, message=message)


@router.post("/workflows/{workflow_id}/retry", response_model=TemporalActionResponse)
async def retry_workflow(
    workflow_id: str,
    body: TemporalRetryRequest,
    db: AsyncSession = Depends(get_db),
    _: Admin = Depends(require_deploy_tracker_admin),
):
    try:
        new_run_id = await svc.retry(db, workflow_id, body.run_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return TemporalActionResponse(ok=True, workflow_id=workflow_id, run_id=new_run_id, message="started new run")


@router.get("/stats", response_model=TemporalStats)
async def get_stats(_: Admin = Depends(require_deploy_tracker_admin)):
    try:
        return await svc.stats()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Temporal unavailable: {e}")


@router.get("/worker", response_model=TemporalWorkerInfo)
async def get_worker(_: Admin = Depends(require_deploy_tracker_admin)):
    try:
        return await svc.worker_info()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Temporal unavailable: {e}")


@router.get("/attention", response_model=TemporalAttentionResponse)
async def get_attention(
    db: AsyncSession = Depends(get_db),
    _: Admin = Depends(require_deploy_tracker_admin),
):
    try:
        items = await svc.find_attention(db)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Temporal unavailable: {e}")
    return TemporalAttentionResponse(
        count=len(items),
        alert_after_min=settings.temporal_stuck_alert_after_min,
        no_progress_min=settings.temporal_stuck_no_progress_min,
        items=items,
    )
