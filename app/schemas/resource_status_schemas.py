"""
Resource Status Schemas

Request/response models for the bulk resource status polling API.
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class ResourceRef(BaseModel):
    table_name: str = Field(..., description="Source table: SERVICE_CONFIG or INFRASTRUCTURE")
    code: str = Field(..., description="Resource code (service_config.code or infrastructure_mst.code)")


class ResourceStatusRequest(BaseModel):
    resources: List[ResourceRef] = Field(
        ...,
        max_length=500,
        description="List of resource references (max 500)"
    )


class ResourceStatusItem(BaseModel):
    table_name: str
    code: str
    status: str
    status_updated_at: Optional[str] = None
    deployment_status: Optional[str] = None
    deployment_error_message: Optional[str] = None
    iac_locked_at: Optional[str] = None

    # Live application state read from ArgoCD. All null when the tenant has no
    # ArgoCD configured for the environment, when the service has no Argo
    # Application (ECS, Vercel, not deployed yet), or when ArgoCD is
    # unreachable and the last-known-good window has passed. The UI falls back
    # to `status` in every one of those cases.
    argocd_sync_status: Optional[str] = Field(
        None, description="Synced | OutOfSync | Unknown"
    )
    argocd_health_status: Optional[str] = Field(
        None, description="Healthy | Progressing | Degraded | Suspended | Missing | Unknown"
    )
    argocd_url: Optional[str] = Field(
        None, description="Deep link to this application in the ArgoCD UI"
    )
    argocd_as_of: Optional[str] = Field(
        None, description="When the ArgoCD state was fetched; older than a minute means stale"
    )
    argocd_state: Optional[str] = Field(
        None,
        description=(
            "ok | not_found | unavailable | not_configured. Set for services only; "
            "says why sync/health are null so the UI can explain itself instead of "
            "blaming a fetch failure for a service that was never deployed."
        ),
    )

    app_url: Optional[str] = Field(None, description="Service URL (shared ALB + service path)")
    health_url: Optional[str] = Field(None, description="Service URL + its health path")


class ResourceStatusResponse(BaseModel):
    resources: List[ResourceStatusItem]


# ── Queue status polling ──────────────────────────────────────────────────────

class QueueStatusRequest(BaseModel):
    queue_codes: List[str] = Field(
        ...,
        max_length=500,
        description="List of transaction queue codes (max 500)"
    )


class QueueStatusItem(BaseModel):
    queue_code: str
    queue_status: Optional[str] = None
    status_last_updated_at: Optional[str] = None


class QueueStatusResponse(BaseModel):
    resources: List[QueueStatusItem]
