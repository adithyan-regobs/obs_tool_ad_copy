from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime
from app.core.enum import PRStatusEnum, WorkflowSourceTableEnum


class GitopsWorkflowDetailInfo(BaseModel):
    """
    Response schema for GitOps workflow detail information.

    Used to provide PR and workflow execution details in API responses.
    """
    id: int = Field(..., description="Workflow record ID")
    code: str = Field(..., description="Unique workflow code")
    name: str = Field(..., description="Workflow name")
    git_repository: Optional[str] = Field(None, description="GitHub repository")
    git_branch: Optional[str] = Field(None, description="Feature branch name")
    git_commit_sha: Optional[str] = Field(None, description="Git commit SHA")
    pr_number: Optional[int] = Field(None, description="GitHub pull request number")
    pr_url: Optional[str] = Field(None, description="Direct URL to pull request")
    pr_status: Optional[PRStatusEnum] = Field(None, description="PR status: PR_OPEN, PR_MERGED, PR_CLOSED")
    workflow_run_id: Optional[str] = Field(None, description="GitHub Actions workflow run ID")
    workflow_run_url: Optional[str] = Field(None, description="Direct URL to workflow logs")
    run_initiated_at: Optional[datetime] = Field(None, description="When workflow started")
    run_completed_at: Optional[datetime] = Field(None, description="When workflow completed")
    created_at: Optional[datetime] = Field(None, description="When record was created")
    transaction_code: Optional[str] = Field(None, description="Code of the entity that created this workflow")
    table_name: Optional[WorkflowSourceTableEnum] = Field(None, description="Source table name for polymorphic reference")

    class Config:
        from_attributes = True  # Pydantic v2 (was orm_mode in v1)


class GitopsWorkflowListItem(BaseModel):
    """
    Response schema for GitOps workflow list item.

    Used in PR listing page with user details from joined user_mst table.
    """
    id: int = Field(..., description="Workflow record ID")
    code: str = Field(..., description="Unique workflow code")
    name: str = Field(..., description="Workflow name")
    git_repository: Optional[str] = Field(None, description="GitHub repository")
    git_branch: Optional[str] = Field(None, description="Feature branch name")
    git_commit_sha: Optional[str] = Field(None, description="Git commit SHA")
    pr_number: Optional[int] = Field(None, description="GitHub pull request number")
    pr_url: Optional[str] = Field(None, description="Direct URL to pull request")
    pr_status: Optional[PRStatusEnum] = Field(None, description="PR status: PR_OPEN, PR_MERGED, PR_CLOSED")
    workflow_run_id: Optional[str] = Field(None, description="GitHub Actions workflow run ID")
    workflow_run_url: Optional[str] = Field(None, description="Direct URL to workflow logs")
    run_initiated_at: Optional[datetime] = Field(None, description="When workflow started")
    run_completed_at: Optional[datetime] = Field(None, description="When workflow completed")
    created_at: Optional[datetime] = Field(None, description="When record was created")
    tenant_mst_code: Optional[str] = Field(None, description="Tenant code")
    user_mst_code: Optional[str] = Field(None, description="User code who created")
    user_email: Optional[str] = Field(None, description="User email from user_mst table")
    user_name: Optional[str] = Field(None, description="User name from user_mst table")
    transaction_code: Optional[str] = Field(None, description="Code of the entity that created this workflow")
    table_name: Optional[WorkflowSourceTableEnum] = Field(None, description="Source table name for polymorphic reference")
    service_name: Optional[str] = Field(None, description="Service name (if applicable)")
    service_code: Optional[str] = Field(None, description="Service code (if applicable)")
    case_type: Optional[str] = Field(None, description="Case type (for infrastructure workflows only)")

    class Config:
        from_attributes = True


class GitopsWorkflowListResponse(BaseModel):
    """
    Paginated response for GitOps workflow listing.
    """
    items: List[GitopsWorkflowListItem] = Field(..., description="List of workflows")
    total: int = Field(..., description="Total count of workflows matching filters")
    page: int = Field(..., description="Current page number")
    limit: int = Field(..., description="Items per page")
    pages: int = Field(..., description="Total pages")


class PRHistoryResponse(BaseModel):
    """
    Paginated response for PR history lookup by polymorphic reference.

    Used by PR History Modal to show all PRs for a specific entity
    (service_config for Terragrunt, dockerfile_workflow_code for Dockerfile).
    """
    items: List[GitopsWorkflowListItem] = Field(..., description="List of PR history items")
    total: int = Field(..., description="Total count of PRs")
    page: int = Field(..., description="Current page number")
    limit: int = Field(..., description="Items per page")
    pages: int = Field(..., description="Total pages")
    transaction_code: str = Field(..., description="The transaction code used for lookup")
    table_name: WorkflowSourceTableEnum = Field(..., description="The table name used for lookup")


class ServiceOption(BaseModel):
    """Service option for filter dropdown"""
    service_code: str = Field(..., description="Service code")
    service_name: str = Field(..., description="Service name")

    class Config:
        from_attributes = True


class CaseTypeOption(BaseModel):
    """Case type option for filter dropdown"""
    case_type: str = Field(..., description="Case type (e.g., 's3-bucket', 'sqs-queue')")

    class Config:
        from_attributes = True
