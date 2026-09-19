"""Response models for the Temporal view of the Admin Dashboard
(/temporal/dashboard/*). Field names match the frontend's
src/types/temporal.ts one-for-one (snake_case here, camelCase there)."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.schemas.deployment_history_schemas import DeploymentStage


class TemporalWorkflowItem(BaseModel):
    """One run as the list shows it. Stage comes from pipeline_run_track
    (DB) for now — see temporal_dashboard_service for the search-attribute
    follow-up that would make it Temporal-native."""
    workflow_id: str
    run_id: str
    workflow_type: str
    # RUNNING | COMPLETED | FAILED | CANCELLED | TERMINATED | TIMED_OUT | CONTINUED_AS_NEW
    status: str
    task_queue: Optional[str] = None
    tenant_code: Optional[str] = None
    user_code: Optional[str] = None
    environment: str  # stage | prod
    stage: Optional[str] = None
    stage_since: Optional[str] = None
    start_time: str
    close_time: Optional[str] = None
    execution_time_seconds: int
    history_length: int
    parent_workflow_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    pr_number: Optional[int] = None
    queue_codes: Optional[str] = None


class TemporalWorkflowListResponse(BaseModel):
    count: int
    items: List[TemporalWorkflowItem]


class TemporalHistoryEvent(BaseModel):
    event_id: int
    event_type: str  # PascalCase without the EventType prefix, e.g. ActivityTaskScheduled
    category: str  # workflow | activity | timer | signal | child | marker | task
    timestamp: str
    summary: Optional[str] = None
    attributes: Dict[str, Any]
    is_failure: bool = False


class TemporalPendingActivity(BaseModel):
    activity_id: str
    activity_type: str
    state: str  # SCHEDULED | STARTED | CANCEL_REQUESTED
    attempt: int
    maximum_attempts: int
    scheduled_time: Optional[str] = None
    last_started_time: Optional[str] = None
    last_heartbeat_time: Optional[str] = None
    last_failure: Optional[str] = None
    last_worker_identity: Optional[str] = None


class TemporalWorkflowDetail(TemporalWorkflowItem):
    input: Any = None
    result: Any = None
    failure: Optional[str] = None
    search_attributes: Dict[str, Any] = {}
    pending_activities: List[TemporalPendingActivity] = []
    history: List[TemporalHistoryEvent] = []
    child_workflow_ids: List[str] = []
    prior_run_ids: List[str] = []
    # Every stage this deploy has reached, oldest first, from
    # pipeline_run_track.build_stages (same entries the Deploy Tracker shows).
    # Empty for the coordinator and anything that never wrote a stage.
    stages: List[DeploymentStage] = []


class TemporalStateResponse(BaseModel):
    workflow_id: str
    run_id: str
    state: Any


class TemporalActionResponse(BaseModel):
    ok: bool
    workflow_id: str
    run_id: Optional[str] = None
    message: str


class TemporalStats(BaseModel):
    running: int
    completed_24h: int
    failed_24h: int
    timed_out_24h: int
    terminated_24h: int
    cancelled_24h: int


class TemporalWorkerInfo(BaseModel):
    task_queue: str
    namespace: str
    status: str  # HEALTHY | OFFLINE
    identity: Optional[str] = None
    workflow_pollers: int
    activity_pollers: int
    last_access_time: Optional[str] = None
    workflow_types: List[str]


class TemporalTerminateRequest(BaseModel):
    run_id: Optional[str] = None
    reason: Optional[str] = None


class TemporalRetryRequest(BaseModel):
    run_id: Optional[str] = None


class TemporalLockHolder(BaseModel):
    """One directory a queued deploy is waiting for, and who holds it."""
    dir: str
    holder_workflow_id: Optional[str] = None
    holder_status: Optional[str] = None  # RUNNING | COMPLETED | … | None = nobody holds it
    # True when this wait needs an admin: the holder is in a closed state, or
    # every dir the waiter needs is free yet it is still queued. A free dir
    # alongside held ones is normal (all-or-nothing granting) and is not stale.
    stale: bool


class TemporalAttentionItem(BaseModel):
    """A running deploy that is genuinely stuck, per
    temporal_dashboard_service.find_attention:
      stale_lock         — waiting in queue on a lock whose holder is no longer running
      activity_retrying  — a pending activity is on attempt 2+
      no_progress        — stage unchanged for > TEMPORAL_STUCK_NO_PROGRESS_MIN
    A deploy queued behind a *running* deploy is normal and is not listed.
    The workflows raise the admin Slack alert themselves (report_stale_queue_wait)."""
    workflow: TemporalWorkflowItem
    reason: str
    since: str
    waiting_for: List[str] = []
    holders: List[TemporalLockHolder] = []


class TemporalAttentionResponse(BaseModel):
    count: int
    alert_after_min: int
    no_progress_min: int
    items: List[TemporalAttentionItem]
