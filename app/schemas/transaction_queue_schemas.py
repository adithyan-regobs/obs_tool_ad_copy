"""
GitOps Queue Schemas

Pydantic schemas for the deploy queue feature.
Used for API request/response validation.
"""

from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from enum import Enum

from app.core.enum import WorkflowSourceTableEnum
from app.db.models.transaction_queue_model import TransactionQueueStatusEnum


# =============================================================================
# Helpers
# =============================================================================

def _normalize_environment_in_snapshot(snapshot: Any) -> Any:
    if not isinstance(snapshot, dict):
        return snapshot
    environment = snapshot.get("environment")
    if isinstance(environment, str) and environment.lower() in ("staging", "stage"):
        return {**snapshot, "environment": "stage"}
    return snapshot


# =============================================================================
# Request Schemas
# =============================================================================

class AddToQueueRequest(BaseModel):
    """
    Request schema for adding any item type to queue.

    Frontend provides:
    - transaction_code: Optional source entity code
    - table_name: Optional source table enum
    - config_snapshot: Configuration parameters
    - case_ref_code: Optional case reference
    - queue_code: Optional - if provided, updates existing; otherwise creates new

    Backend extracts from JWT:
    - user_code
    - tenant_code
    """
    transaction_code: Optional[str] = Field(
        None,
        description="Code of the source entity (service_config.code, infrastructure_mst.code, etc.)"
    )
    table_name: Optional[WorkflowSourceTableEnum] = Field(
        None,
        description="Source table: SERVICE_CONFIG, INFRASTRUCTURE, ALERT_CONFIG, KONG_ROUTE, PIPELINE, SERVICE_CONFIG_DOCKERFILE"
    )
    config_snapshot: Dict[str, Any] = Field(
        ...,
        description="Configuration parameters from frontend (will be enriched by backend)"
    )
    case_ref_code: Optional[str] = Field(
        None,
        description="Optional case reference code"
    )
    ticket_code: Optional[str] = Field(
        None,
        description="Optional ticket code reference"
    )
    queue_code: Optional[str] = Field(
        None,
        description="Optional queue code - if provided, updates existing item; otherwise creates new"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "transaction_code": "infra-mst-abc123",
                "table_name": "INFRASTRUCTURE",
                "config_snapshot": {
                    "identifier": "my-s3-bucket",
                    "infra_type": "s3",
                    "environment": "dev",
                    "applications_mst_code": "app-xyz",
                    "geo_loc_mst_code": "region-mumbai",
                    "versioning": True
                },
                "case_ref_code": "case-s3-001"
            }
        }


class TransactionQueueDeployRequest(BaseModel):
    """
    Request schema for deploying all pending queue items.

    Optional filters can limit which items to deploy.
    """
    environment: Optional[str] = Field(
        None,
        description="Only deploy items for this environment"
    )
    item_ids: Optional[List[int]] = Field(
        None,
        description="Only deploy specific item IDs (if provided)"
    )
    service_config_code: Optional[str] = Field(
        None,
        description=(
            "The service_configs code being deployed. REQUIRED in practice: "
            "the route's Authorization card reads can_deploy from this field, "
            "and a missing one is a 422 before the handler runs. Every id in "
            "item_ids must belong to it — the card authorizes one object while "
            "the ids travel separately, so they are held to the object that "
            "was actually authorized."
        ),
    )

    class Config:
        json_schema_extra = {
            "example": {
                "environment": "dev"
            }
        }


class TransactionQueuePRRefreshRequest(BaseModel):
    """
    Request schema for refreshing a stale PR.

    Regenerates the PR from stored snapshots to resolve conflicts.
    """
    pr_number: int = Field(
        ...,
        description="PR number to refresh"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "pr_number": 123
            }
        }


# =============================================================================
# Response Schemas
# =============================================================================

class TicketDetailsResponse(BaseModel):
    """
    Response schema for ticket details.
    """
    id: int = Field(..., description="Ticket ID")
    code: str = Field(..., description="Ticket code")
    ticket_number: str = Field(..., description="Globally unique ticket number")
    source: Optional[str] = Field(None, description="Source system where ticket originated")
    source_ref_id: Optional[str] = Field(None, description="Reference ID from source system")
    user_mst_code: str = Field(..., description="Assigned user code")

    class Config:
        from_attributes = True


class TransactionQueueItemResponse(BaseModel):
    """
    Response schema for a single queue item.
    """
    id: int = Field(..., description="Queue item ID")
    code: str = Field(..., description="Queue item code")
    user_code: str = Field(..., description="User who added this item")
    transaction_code: Optional[str] = Field(None, description="Source entity code")
    table_name: Optional[WorkflowSourceTableEnum] = Field(None, description="Source table enum")
    config_snapshot: Dict[str, Any] = Field(..., description="Configuration snapshot")
    display_name: Optional[str] = Field(None, description="Formatted display name for UI")
    case_ref_code: Optional[str] = Field(None, description="Case reference code")
    ticket_code: Optional[str] = Field(None, description="Ticket reference code")
    status: TransactionQueueStatusEnum = Field(..., description="Queue item status")
    status_last_updated_at: datetime = Field(..., description="When status was last updated")
    tenant_code: Optional[str] = Field(None, description="Tenant code")
    created_at: datetime = Field(..., description="When added to queue")
    updated_at: datetime = Field(..., description="Last updated")
    # Pipeline run track status (from latest pipeline_run_track via transaction_code)
    pipeline_run_status: Optional[str] = Field(None, description="Latest pipeline run status (PENDING, RUNNING, COMPLETED, FAILED)")
    pipeline_build_stages: Optional[Any] = Field(None, description="Build stage breakdown from pipeline_run_track")
    pipeline_run_started_at: Optional[str] = Field(None, description="Pipeline run created_at timestamp (trigger time)")
    pipeline_deploy_result: Optional[Any] = Field(None, description="Latest pipeline run deploy_result (prs, alb_url, error) — used to render the PR link on gateway deploy cards")
    service_mst_code: Optional[str] = Field(
        None,
        description=(
            "The service a KONG_ROUTE item belongs to, resolved server-side. "
            "transaction_code cannot say it on its own — it points at either a route "
            "(KRC_) or a route group (KRG_), and neither is a service. Filter gateway "
            "items on this rather than on transaction_code."
        ),
    )

    class Config:
        from_attributes = True

    @field_validator("config_snapshot", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> Any:
        return _normalize_environment_in_snapshot(value)


class TransactionQueueItemWithTicketResponse(BaseModel):
    """
    Response schema for a single queue item with ticket details included.
    Used for search endpoints where ticket relationship is eagerly loaded.
    """
    id: int = Field(..., description="Queue item ID")
    code: str = Field(..., description="Queue item code")
    user_code: str = Field(..., description="User who added this item")
    transaction_code: Optional[str] = Field(None, description="Source entity code")
    table_name: Optional[WorkflowSourceTableEnum] = Field(None, description="Source table enum")
    config_snapshot: Dict[str, Any] = Field(..., description="Configuration snapshot")
    display_name: Optional[str] = Field(None, description="Formatted display name for UI")
    case_ref_code: Optional[str] = Field(None, description="Case reference code")
    ticket_code: Optional[str] = Field(None, description="Ticket reference code")
    ticket: Optional[TicketDetailsResponse] = Field(None, description="Ticket details")
    status: TransactionQueueStatusEnum = Field(..., description="Queue item status")
    status_last_updated_at: datetime = Field(..., description="When status was last updated")
    tenant_code: Optional[str] = Field(None, description="Tenant code")
    created_at: datetime = Field(..., description="When added to queue")
    updated_at: datetime = Field(..., description="Last updated")

    class Config:
        from_attributes = True

    @field_validator("config_snapshot", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> Any:
        return _normalize_environment_in_snapshot(value)


class TransactionQueueListResponse(BaseModel):
    """
    Paginated response for queue item listing.
    """
    items: List[TransactionQueueItemResponse] = Field(..., description="Queue items")
    total: int = Field(..., description="Total count")
    pending_count: int = Field(..., description="Count of pending items")
    pr_raised_count: int = Field(..., description="Count of items with PR raised")

    class Config:
        from_attributes = True


class TransactionQueuePreviewResponse(BaseModel):
    """
    Response schema for HCL preview.

    Shows what will be generated from the stored snapshot.
    """
    id: int = Field(..., description="Queue item ID")
    hcl_content: str = Field(..., description="Generated HCL content")
    file_path: str = Field(..., description="File path in repo")
    atlantis_project_name: str = Field(..., description="Atlantis project name")
    service_name: Optional[str] = Field(None, description="Service name")
    environment: str = Field(..., description="Environment")
    infra_type: str = Field(..., description="Infrastructure type")


class DeployItemResult(BaseModel):
    """
    Result for a single item in the deploy operation.
    """
    id: int = Field(..., description="Queue item ID")
    service_config_code: Optional[str] = Field(None, description="Service config code")
    status: str = Field(..., description="Result status: success, error, skipped")
    error: Optional[str] = Field(None, description="Error message if failed")
    file_path: str = Field(..., description="HCL file path")


class TransactionQueueDeployResponse(BaseModel):
    """
    Response schema for deploy operation.
    """
    status: str = Field(..., description="Overall status: success, partial, error, queued")
    workflow_id: Optional[str] = Field(None, description="Temporal workflow ID (Temporal/Atlantis flow only)")
    pr_number: Optional[int] = Field(None, description="Created PR number")
    pr_url: Optional[str] = Field(None, description="Created PR URL")
    git_branch: Optional[str] = Field(None, description="Feature branch name")
    commit_sha: Optional[str] = Field(None, description="Commit SHA")
    items_deployed: int = Field(..., description="Count of successfully deployed items")
    items_failed: int = Field(..., description="Count of failed items")
    items_skipped: int = Field(0, description="Count of skipped items")
    details: List[DeployItemResult] = Field(..., description="Per-item results")
    error: Optional[str] = Field(None, description="Overall error message if failed")


class TransactionQueuePRStatusResponse(BaseModel):
    """
    Response schema for PR status check.

    Shows if PR is stale and needs refresh.
    """
    pr_number: int = Field(..., description="PR number")
    pr_url: str = Field(..., description="PR URL")
    status: str = Field(..., description="Branch comparison status")
    is_stale: bool = Field(..., description="True if PR needs refresh")
    behind_by: int = Field(..., description="Commits behind base branch")
    ahead_by: int = Field(..., description="Commits ahead of base branch")
    base_branch: str = Field(..., description="Base branch name")
    head_branch: str = Field(..., description="Feature branch name")
    items_count: int = Field(..., description="Number of queue items in this PR")


class TransactionQueuePRRefreshResponse(BaseModel):
    """
    Response schema for PR refresh operation.
    """
    status: str = Field(..., description="Refresh status: success, error")
    pr_number: int = Field(..., description="PR number (may be new if original was closed)")
    pr_url: Optional[str] = Field(None, description="PR URL (new URL if PR was recreated)")
    commit_sha: Optional[str] = Field(None, description="New commit SHA")
    items_regenerated: int = Field(..., description="Count of items regenerated")
    error: Optional[str] = Field(None, description="Error message if failed")


class TransactionQueuePRListItem(BaseModel):
    """
    Response schema for a PR in the queue PR listing.
    """
    pr_number: int = Field(..., description="PR number")
    pr_url: str = Field(..., description="PR URL")
    git_branch: str = Field(..., description="Feature branch name")
    status: str = Field(..., description="PR status")
    items_count: int = Field(..., description="Number of items in this PR")
    environments: List[str] = Field(..., description="Environments included")
    infra_types: List[str] = Field(..., description="Infrastructure types included")
    items: List[TransactionQueueItemResponse] = Field(..., description="Queue items in this PR")
    is_stale: Optional[bool] = Field(None, description="True if PR needs refresh")
    created_at: datetime = Field(..., description="When PR was created")
    user_name: Optional[str] = Field(None, description="User who created the PR")


class TransactionQueuePRListResponse(BaseModel):
    """
    Response schema for listing PRs from deploy queue.
    """
    items: List[TransactionQueuePRListItem] = Field(..., description="PR list items")
    total: int = Field(..., description="Total count of PRs")


class DeleteQueueItemResponse(BaseModel):
    """
    Response schema for deleting a queue item.
    """
    id: int = Field(..., description="Deleted item ID")
    status: str = Field(..., description="New status (deleted)")
    message: str = Field(..., description="Success message")


class SearchQueueRequest(BaseModel):
    """
    Request schema for searching queue items by transaction code and table name.

    Searches for the latest DRAFT/APPROVED status queue item matching the criteria.
    """
    transaction_code: str = Field(
        ...,
        description="Code of the source entity (service_config.code, infrastructure_mst.code, etc.)"
    )
    table_name: WorkflowSourceTableEnum = Field(
        ...,
        description="Source table: SERVICE_CONFIG, INFRASTRUCTURE, ALERT_CONFIG, KONG_ROUTE, PIPELINE, SERVICE_CONFIG_DOCKERFILE"
    )
    case_ref_code: Optional[str] = Field(
        None,
        description="Optional case reference code to narrow the search (e.g. 'user_management' vs 'database_creation')"
    )
    include_failed: bool = Field(
        False,
        description="When true, also matches FAILED items so a failed deploy can be found and retried (redeploy path only)."
    )

    class Config:
        json_schema_extra = {
            "example": {
                "transaction_code": "infra-mst-abc123",
                "table_name": "INFRASTRUCTURE",
                "case_ref_code": "user_management"
            }
        }


class SearchQueueResponse(BaseModel):
    """
    Response schema for queue search.

    Returns the latest DRAFT queue item if found, otherwise null.
    Includes ticket details when available.
    """
    found: bool = Field(..., description="Whether a matching draft queue item was found")
    item: Optional[TransactionQueueItemWithTicketResponse] = Field(
        None,
        description="The latest draft queue item with ticket details if found, null otherwise"
    )


class SearchByTicketRequest(BaseModel):
    """
    Request schema for searching queue items by ticket code.

    Searches for the latest APPROVED status queue item matching the ticket_code.
    """
    ticket_code: str = Field(
        ...,
        description="Ticket code to search for associated queue items"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "ticket_code": "ticket-abc123"
            }
        }


class ConflictResolveRequest(BaseModel):
    """
    Request schema for resolving conflicts on an existing PR.

    Rebases the feature branch and regenerates files from stored snapshots.
    PR number is provided as a path parameter.
    """
    git_repository: str = Field(
        ...,
        description="GitHub repository in 'owner/repo' format (e.g., 'Regobs/Devlift-Pipeline')"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "git_repository": "Regobs/Devlift-Pipeline"
            }
        }


class ConflictResolveResponse(BaseModel):
    """
    Response schema for conflict resolve operation.
    """
    status: str = Field(..., description="Resolve status: success, error")
    pr_number: int = Field(..., description="PR number")
    git_repository: str = Field(..., description="GitHub repository (owner/repo)")
    feature_branch: str = Field(..., description="Feature branch name")
    base_branch: str = Field(..., description="Base branch name")
    commit_sha: Optional[str] = Field(None, description="New commit SHA after regeneration")
    queue_count: int = Field(..., description="Number of queue items regenerated")
    error: Optional[str] = Field(None, description="Error message if failed")


class BulkApproveRequest(BaseModel):
    """
    Request schema for bulk approving queue items.

    Accepts a list of queue IDs to update their status to APPROVED.
    """
    queue_ids: List[int] = Field(
        ...,
        description="List of queue item IDs to approve",
        min_length=1
    )

    class Config:
        json_schema_extra = {
            "example": {
                "queue_ids": [1, 2, 3, 4, 5]
            }
        }


class BulkApproveResponse(BaseModel):
    """
    Response schema for bulk approval operation.
    """
    status: str = Field(..., description="Operation status: success or partial")
    updated_count: int = Field(..., description="Number of items successfully updated")
    requested_count: int = Field(..., description="Number of items requested to update")
    message: str = Field(..., description="Success or error message")
