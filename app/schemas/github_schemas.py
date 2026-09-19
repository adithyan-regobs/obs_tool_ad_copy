from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime


class RepositoryItem(BaseModel):
    """Individual repository item from GitHub"""
    id: int = Field(..., description="GitHub repository ID")
    name: str = Field(..., description="Repository name")
    full_name: str = Field(..., description="Full repository name (owner/repo)")
    description: Optional[str] = Field(None, description="Repository description")
    private: bool = Field(..., description="Whether repository is private")
    default_branch: str = Field(..., description="Default branch name (usually 'main' or 'master')")
    html_url: str = Field(..., description="Repository URL")
    updated_at: str = Field(..., description="Last updated timestamp")


class RepositoriesListResponse(BaseModel):
    """Response for repositories list endpoint"""
    repositories: List[RepositoryItem] = Field(default=[], description="List of repositories")
    total_count: int = Field(..., description="Total number of repositories returned")


class BranchCommit(BaseModel):
    """Commit information for a branch"""
    sha: str = Field(..., description="Commit SHA")


class BranchItem(BaseModel):
    """Individual branch item from GitHub"""
    name: str = Field(..., description="Branch name")
    protected: bool = Field(..., description="Whether branch is protected")


class BranchesListResponse(BaseModel):
    """Response for branches list endpoint"""
    branches: List[BranchItem] = Field(default=[], description="List of branches")
    total_count: int = Field(..., description="Total number of branches")


class CommitTerragruntRequest(BaseModel):
    """
    Request for committing a terragrunt file to GitHub

    Note: tenant is NOT included in request - it's automatically extracted
    from JWT authentication and passed separately to the service layer.
    """
    terragrunt_content: str = Field(..., description="Terragrunt HCL file content", min_length=1)
    environment: str = Field(..., description="Environment name (e.g., dev, staging, prod)", min_length=1)
    github_repository: str = Field(..., description="GitHub repository in format 'owner/repo'", min_length=1)
    branch_name: str = Field(..., description="Target branch name", min_length=1)
    commit_message: Optional[str] = Field(None, description="Custom commit message (auto-generated if not provided)")
    resource_type: Optional[str] = Field(None, description="Resource type: add_route, create_bucket, create_queue, table_management, mysql_user_management, postgresql_user_management, user_management, database_creation")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Service-specific parameters (e.g., for gateway: api_name, method, route)")
    product_name: Optional[str] = Field(None, description="Product name for PR workflow path construction (required for datadog/gateway)")
    product_code: Optional[str] = Field(None, description="Product/application code (used as application_code for infrastructure resources)")
    resource_group_code: Optional[str] = Field(None, description="Resource group code (optional, used for infrastructure resources)")
    region: Optional[str] = Field(None, description="Region for PR workflow path construction (required for datadog/gateway)")
    infra_vendor: Optional[str] = Field(None, description="Infrastructure vendor for chat context (aws/gcp/azure/on_prem)")
    service_code: Optional[str] = Field(None, description="Service code for chat context (optional)")
    geo_loc_code: Optional[str] = Field(None, description="Geographic location code (e.g., 'mumbai', 'london', 'uk')")
    case_type_ref_code: Optional[str] = Field(None, description="Case type reference code for chat context (e.g., 'sqs', 'kong_gateway')")


class CommitTerragruntResponse(BaseModel):
    """Response for terragrunt commit operation"""
    success: bool = Field(..., description="Whether the commit was successful")
    commit_sha: Optional[str] = Field(None, description="Git commit SHA")
    file_path: str = Field(..., description="Path where file was committed")
    commit_url: Optional[str] = Field(None, description="URL to view the commit")
    html_url: Optional[str] = Field(None, description="URL to view the file")
    message: str = Field(..., description="Result message")


class GetTerragruntContentRequest(BaseModel):
    """Request for rendering terragrunt content preview"""
    resource_type: str = Field(
        ...,
        description="Resource type: create_bucket, create_queue, table_management, add_route, database_creation"
    )
    parameters: Dict[str, Any] = Field(
        ...,
        description="Resource-specific parameters"
    )
    condition: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional condition for conditional rendering. E.g., for database_creation: {'type': 'mysql'} or {'type': 'postgresql'}"
    )


class GetTerragruntContentResponse(BaseModel):
    """Response containing rendered terragrunt content"""
    success: bool = Field(..., description="Whether rendering was successful")
    terragrunt_content: str = Field(..., description="Rendered HCL content")
    resource_type: str = Field(..., description="Resource type that was rendered")
    message: str = Field(..., description="Result message")


class PRStatusCheckResponse(BaseModel):
    """Response for PR status check endpoint"""
    pr_number: int = Field(..., description="GitHub pull request number")
    pr_url: Optional[str] = Field(None, description="Direct URL to pull request")
    pr_status: Optional[str] = Field(None, description="PR status: PR_OPEN, PR_MERGED, PR_CLOSED")
    pr_title: Optional[str] = Field(None, description="PR title")
    git_branch: Optional[str] = Field(None, description="Feature branch name")
    atlantis_terragrunt_status: Optional[str] = Field(
        None,
        description="Atlantis terragrunt status: PLAN_SUCCESS, PLAN_FAILED, or null if not found"
    )


class BulkPRStatusSyncRequest(BaseModel):
    """Request for bulk PR status sync using workflow codes"""
    workflow_codes: List[str] = Field(..., description="List of gitops_workflow_detail codes to sync", min_length=1)


class BulkPRStatusSyncResultItem(BaseModel):
    """Result for a single workflow in the bulk sync response"""
    workflow_code: str = Field(..., description="Gitops workflow detail code")
    repository: Optional[str] = Field(None, description="GitHub repository (owner/repo)")
    pr_number: Optional[int] = Field(None, description="GitHub pull request number")
    previous_status: Optional[str] = Field(None, description="Previous PR status in DB")
    new_status: Optional[str] = Field(None, description="Updated PR status from GitHub")
    updated: bool = Field(..., description="Whether the status was updated")
    error: Optional[str] = Field(None, description="Error message if sync failed for this item")


class BulkPRStatusSyncResponse(BaseModel):
    """Response for bulk PR status sync"""
    results: List[BulkPRStatusSyncResultItem] = Field(default=[], description="Per-item sync results")
    total: int = Field(..., description="Total items processed")
    updated_count: int = Field(..., description="Number of items whose status was updated")
