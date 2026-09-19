from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class DeploymentResource(BaseModel):
    queue_id: int
    queue_code: str
    display_name: str
    queue_status: str
    # Polymorphic reference to the underlying resource, so the UI can deep-link
    # to that resource's Deployments tab (matches the canvas node's
    # resourceDbCode = service_config.code / infrastructure_mst.code).
    transaction_code: Optional[str] = None
    table_name: Optional[str] = None
    # The resource's infrastructuretype_ref code (s3/sqs/dynamodb/ecs/eks…) —
    # lets the UI label an INFRASTRUCTURE item as "S3 update" instead of the
    # generic "Infrastructure update".
    resource_type: Optional[str] = None
    # What KIND of change the queue item was (e.g. "add_route" for gateway
    # rows). table_name can no longer say: gateway rows live on SERVICE_CONFIG
    # too, and only case_ref_code tells them apart.
    case_ref_code: Optional[str] = None


class DeploymentHistoryItem(BaseModel):
    workflow_id: str
    status: str  # RUNNING, COMPLETED, FAILED, TIMED_OUT, TERMINATED, CANCELLED
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    user_code: Optional[str] = None
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    application_code: Optional[str] = None
    application_name: Optional[str] = None
    environment: Optional[str] = None
    geo_code: Optional[str] = None
    geo_name: Optional[str] = None
    vendor: Optional[str] = None  # cloud vendor: aws | gcp | azure | on_prem
    resources: list[DeploymentResource] = []


class DeploymentHistoryResponse(BaseModel):
    items: list[DeploymentHistoryItem]
    total: int


class DeploymentStage(BaseModel):
    """One pipeline stage from pipeline_run_track.build_stages
    ('{artifact}: {action}' naming; duration = ended_at − started_at)."""
    name: str
    status: str  # running | completed | failed
    started_at: str
    ended_at: Optional[str] = None
    error: Optional[str] = None


class DeploymentHistoryDetail(DeploymentHistoryItem):
    """Single-deployment detail (GET /deployments/history/{workflow_id}) —
    the list item plus its merged, deduped pipeline stages. Stages ride only
    here, never on the list, so table pages stay lean."""
    stages: list[DeploymentStage] = []
