"""
Script Preview Schemas

Pydantic schemas for script preview API.
"""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class ScriptFilePreview(BaseModel):
    """
    Preview for a single script file.

    Contains the generated script content along with location metadata.
    """
    branch: str = Field(..., description="Target branch name")
    file_path: str = Field(..., description="Path to file in repository")
    operation: str = Field(..., description="Operation type: create or update")
    script_content: Optional[str] = Field(None, description="Generated script content (HCL)")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        from_attributes = True


class PrPreviewFile(BaseModel):
    """One file of the pull request, normalised for blind rendering.

    The raw script_gen_responses vary per generator ({key: str} vs
    {key: {original_content, preview_content}}), which forced every consumer
    to know each generator's shape. This is the flattened contract the UI
    renders without knowing who generated what."""
    path: str = Field(..., description="Repository file path the PR will touch")
    script_type: str = Field(..., description="Generator key (terragrunt, dockerfile, ...)")
    branch: Optional[str] = Field(None, description="Base branch, when the generator previews per branch")
    repo: Optional[str] = Field(None, description="owner/repo the file lives in — each repo gets its OWN pull request")
    base_branch: Optional[str] = Field(None, description="The branch the PR will target")
    status: Optional[str] = Field(
        None,
        description="added | modified | unchanged | unknown — git's own vocabulary. "
                    "'added' = file absent on the base branch, GitHub renders it "
                    "all-green with no left side; 'unchanged' = byte-identical, "
                    "git will not include it in the PR at all.",
    )
    changed: Optional[bool] = Field(
        None,
        description="Whether this file differs from what is in the repository "
                    "now — only changed files end up in the pull request. None "
                    "when the current content could not be read to compare.",
    )
    before: Optional[str] = Field(None, description="Current content, when the generator distinguishes it")
    after: Optional[str] = Field(None, description="Generated content the PR will carry")
    warning: Optional[str] = Field(None, description="Generator warning, if any")


class ScriptPreviewResponse(BaseModel):
    """
    Response schema for script preview endpoint.

    Contains script generation responses and file location responses.
    """
    queue_id: int = Field(..., description="Queue item ID")
    script_gen_responses: Dict[int, Dict[str, Any]] = Field(
        ...,
        description="Script generation responses per queue_id. Can be either {script_gen_key: content} or {script_gen_key: {branch: {original_content, preview_content}}}"
    )
    file_location_responses: Dict[int, Any] = Field(..., description="File location responses per queue_id")
    files: Optional[List[PrPreviewFile]] = Field(
        None, description="Normalised per-file view of the same data — see PrPreviewFile"
    )

    class Config:
        from_attributes = True


class ScriptPRCreateResponse(BaseModel):
    """
    Response schema for script PR creation endpoint.

    Contains aggregated results from processing multiple queue items,
    including feature branches created, file locations, scripts generated, and PRs.
    """
    feature_branches: Dict[str, Any] = Field(..., description="Feature branches created (repo|||branch -> {branch, type})")
    file_location_responses: Dict[int, Any] = Field(..., description="File location responses per queue_id")
    script_gen_responses: Dict[int, Dict[str, Any]] = Field(
        ...,
        description="Script generation responses per queue_id. Can be either {script_gen_key: content} or {script_gen_key: {branch: {original_content, preview_content}}}"
    )
    gitops_responses: Dict[str, Dict[str, Any]] = Field(..., description="GitOps responses (commits and PRs) per repo|||branch")
    total_items_processed: int = Field(..., description="Total number of queue items processed")
    total_feature_branches: int = Field(..., description="Total number of feature branches created")
    total_prs_created: int = Field(..., description="Total number of PRs created")
    jenkins_results: list = Field(default_factory=list, description="Jenkins pipeline provisioning results (job_name, job_url, status per service)")
    workflow_id: Optional[str] = Field(None, description="Temporal workflow ID (set when routed through Temporal for aspora/vance tenants)")

    class Config:
        from_attributes = True


class ScriptPRCreateRequest(BaseModel):
    """
    Request schema for script PR creation endpoint.

    Allows processing specific queue items or all pending items.
    """
    queue_ids: Optional[List[int]] = Field(None, description="List of specific queue IDs to process")
    all_pending_queues: bool = Field(False, description="If True, process all pending queues for the user")

    class Config:
        from_attributes = True
