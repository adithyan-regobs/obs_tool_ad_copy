"""
Pipeline Management Schemas

This module contains Pydantic schemas for pipeline management API endpoints.
These schemas are used for request validation and response serialization.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Any, Optional, List, Dict
from datetime import datetime
from enum import Enum
from app.core.enum import EnvironmentEnum, PipelineAgentEnum
from app.schemas.gitops_workflow_schemas import GitopsWorkflowDetailInfo


class DeploymentStatus(str, Enum):
    """Deployment status enumeration"""
    SUCCESS = "success"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"
    PENDING = "pending"


class Environment(str, Enum):
    """Environment enumeration"""
    DEVELOPMENT = "development"
    STAGE = "stage"
    STAGING = "staging"
    QA = "qa"
    PRODUCTION = "production"


class CreatePipelineRequest(BaseModel):
    """Request schema for creating a new pipeline"""

    service_code: str = Field(
        ...,
        description="Service code from services_mst",
        min_length=1,
        max_length=100
    )

    pipeline_name: str = Field(
        ...,
        description="Human-readable name for the pipeline",
        min_length=1,
        max_length=255
    )

    github_repository: str = Field(
        ...,
        description="GitHub repository in format: org/repo",
        min_length=1,
        max_length=500
    )

    branch_name: str = Field(
        ...,
        description="Git branch name (e.g., main, develop)",
        min_length=1,
        max_length=100
    )

    environment: EnvironmentEnum = Field(
        ...,
        description="Deployment environment (dev, staging, prod)"
    )

    geo_loc_mst_code: str = Field(
        ...,
        description="Geographic location code from geo_loc_mst (e.g., 'mumbai', 'london')",
        min_length=1,
        max_length=100
    )

    @field_validator("service_code")
    @classmethod
    def validate_service_code(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("service_code cannot be empty or whitespace")
        return v.strip()

    @field_validator("pipeline_name")
    @classmethod
    def validate_pipeline_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("pipeline_name cannot be empty or whitespace")
        return v.strip()

    @field_validator("github_repository")
    @classmethod
    def validate_github_repository(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("github_repository cannot be empty")
        return v

    @field_validator("branch_name")
    @classmethod
    def validate_branch_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("branch_name cannot be empty or whitespace")
        return v.strip()


class CreatePipelineResponse(BaseModel):
    """Response schema for pipeline creation"""

    status: str = Field(
        ...,
        description="Operation status (e.g., 'success', 'partial')"
    )

    message: str = Field(
        ...,
        description="Human-readable message describing the result"
    )

    pipeline_code: str = Field(
        ...,
        description="Generated pipeline code"
    )

    ecr_repo_url: str = Field(
        ...,
        description="AWS ECR repository URI"
    )

    iam_role_arn: str = Field(
        ...,
        description="AWS IAM role ARN for GitHub Actions"
    )

    yaml_content: str = Field(
        ...,
        description="Generated GitHub Actions workflow YAML content"
    )

    github_commit_sha: Optional[str] = Field(
        None,
        description="GitHub commit SHA after pushing workflow file (null if commit failed)"
    )

    workflow_file_path: Optional[str] = Field(
        None,
        description="Path to the workflow file in the repository (e.g., .github/workflows/deploy-service-prod.yml)"
    )

    github_commit_url: Optional[str] = Field(
        None,
        description="Direct URL to view the GitHub commit"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "status": "success",
                "message": "Pipeline created successfully",
                "pipeline_code": "pipeline_01",
                "ecr_repo_url": "123456789012.dkr.ecr.us-east-1.amazonaws.com/acme_us-east-1_prod_payment-api",
                "iam_role_arn": "arn:aws:iam::123456789012:role/GitHubActions_payment_api_acme-payment-service_prod",
                "yaml_content": "name: Deploy Payment API to Production\n...",
                "github_commit_sha": "a1b2c3d4e5f6g7h8i9j0",
                "workflow_file_path": ".github/workflows/deploy-payment-api-prod.yml",
                "github_commit_url": "https://github.com/owner/repo/commit/a1b2c3d4e5f6g7h8i9j0"
            }
        }


class SavePipelineRequest(BaseModel):
    """Request schema for saving pipeline without GitHub operations (onboarding existing pipelines)"""

    service_code: str = Field(
        ...,
        description="Service code from services_mst",
        min_length=1,
        max_length=100
    )

    pipeline_name: str = Field(
        ...,
        description="Human-readable name for the pipeline",
        min_length=1,
        max_length=255
    )

    github_repository: str = Field(
        ...,
        description="GitHub repository in format: org/repo",
        min_length=1,
        max_length=500
    )

    branch_name: str = Field(
        ...,
        description="Git branch name (e.g., main, develop)",
        min_length=1,
        max_length=100
    )

    environment: EnvironmentEnum = Field(
        ...,
        description="Deployment environment (dev, staging, prod)"
    )

    geo_loc_mst_code: str = Field(
        ...,
        description="Geographic location code from geo_loc_mst (e.g., 'mumbai', 'london')",
        min_length=1,
        max_length=100
    )

    workflow_file_path: str = Field(
        ...,
        description="Path to existing workflow file (e.g., .github/workflows/deploy.yml)",
        min_length=1,
        max_length=500
    )

    @field_validator("service_code")
    @classmethod
    def validate_service_code(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("service_code cannot be empty or whitespace")
        return v.strip()

    @field_validator("pipeline_name")
    @classmethod
    def validate_pipeline_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("pipeline_name cannot be empty or whitespace")
        return v.strip()

    @field_validator("github_repository")
    @classmethod
    def validate_github_repository(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("github_repository cannot be empty")
        return v

    @field_validator("branch_name")
    @classmethod
    def validate_branch_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("branch_name cannot be empty or whitespace")
        return v.strip()

    @field_validator("workflow_file_path")
    @classmethod
    def validate_workflow_file_path(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("workflow_file_path cannot be empty or whitespace")
        v = v.strip()

        # If user passes full path (e.g., .github/workflows/deploy.yml), use as-is
        if v.startswith(".github/workflows/"):
            return v

        # If user passes just filename with .yml extension, prepend .github/workflows/
        if v.endswith(".yml") or v.endswith(".yaml"):
            return f".github/workflows/{v}"

        # If user passes just filename without extension, add .yml and prepend path
        return f".github/workflows/{v}.yml"


class SavePipelineResponse(BaseModel):
    """Response schema for saved pipeline (no GitHub operations)"""

    status: str = Field(
        ...,
        description="Operation status (success)"
    )

    message: str = Field(
        ...,
        description="Human-readable message describing the result"
    )

    pipeline_code: str = Field(
        ...,
        description="Generated pipeline code"
    )

    ecr_repo_url: str = Field(
        ...,
        description="AWS ECR repository URI"
    )

    iam_role_arn: str = Field(
        ...,
        description="AWS IAM role ARN for GitHub Actions"
    )

    workflow_file_path: str = Field(
        ...,
        description="Path to workflow file provided by user"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "status": "success",
                "message": "Pipeline saved successfully",
                "pipeline_code": "pipeline_01",
                "ecr_repo_url": "123456789012.dkr.ecr.us-east-1.amazonaws.com/acme_mumbai_prod_payment-api",
                "iam_role_arn": "arn:aws:iam::123456789012:role/GitHubActions_payment_api",
                "workflow_file_path": ".github/workflows/deploy-payment-api-prod.yml"
            }
        }


class GetAllPipelinesRequest(BaseModel):
    """Request schema for getting all pipelines"""
    skip: int = Field(default=0, ge=0, description="Number of records to skip")
    limit: int = Field(default=100, ge=1, le=500, description="Maximum number of records to return")
    environment: Optional[Environment] = Field(None, description="Filter by environment")
    language: Optional[str] = Field(None, description="Filter by programming language")
    deployment_status: Optional[DeploymentStatus] = Field(None, description="Filter by deployment status")


class PipelineListItem(BaseModel):
    """Individual pipeline item in the list"""
    id: int = Field(..., description="Pipeline ID")
    name: str = Field(..., description="Pipeline name")
    environment: str = Field(..., description="Deployment environment")
    last_deployment: str = Field(..., description="Last deployment timestamp (ISO format)")
    deployment_status: str = Field(..., description="Current deployment status")
    build_logs: str = Field(..., description="URL to build logs")
    repo: str = Field(..., description="Repository URL")
    branch: str = Field(..., description="Git branch name")
    language: str = Field(..., description="Programming language")
    version: str = Field(..., description="Language version")
    deployment_number: int = Field(..., description="Number of deployments")


class GetAllPipelinesResponse(BaseModel):
    """Response schema for getting all pipelines"""
    total: int = Field(..., description="Total number of pipelines")
    skip: int = Field(..., description="Number of records skipped")
    limit: int = Field(..., description="Maximum number of records returned")
    pipelines: List[PipelineListItem] = Field(default=[], description="List of pipelines")


class LanguagesResponse(BaseModel):
    """Response schema for supported languages"""
    languages: List[str] = Field(..., description="List of supported programming languages")


class LanguageVersionsResponse(BaseModel):
    """Response schema for language versions"""
    language: str = Field(..., description="Programming language")
    versions: List[str] = Field(..., description="List of supported versions for the language")


class RepositoriesResponse(BaseModel):
    """Response schema for available repositories"""
    repositories: List[str] = Field(..., description="List of available GitHub repositories")


class BranchesResponse(BaseModel):
    """Response schema for available branches"""
    branches: List[str] = Field(..., description="List of available Git branches")


class RunPipelineRequest(BaseModel):
    """Request schema for triggering/running a pipeline"""
    pipeline_code: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Unique code of the pipeline to run"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "pipeline_code": "pipeline_payment_prod_main"
            }
        }


class RunPipelineResponse(BaseModel):
    """Response schema for running a pipeline"""
    status: str = Field(..., description="Status of the operation (success/error)")
    message: str = Field(..., description="Descriptive message about the operation")

    run_code: str = Field(
        ...,
        description="Unique code for this pipeline run"
    )

    run_status: str = Field(
        ...,
        description="Initial status of the pipeline run (PENDING, RUNNING, etc.)"
    )

    commit_sha: Optional[str] = Field(
        None,
        description="GitHub commit SHA that triggered the pipeline"
    )

    commit_url: Optional[str] = Field(
        None,
        description="URL to view the commit on GitHub"
    )

    log_url: Optional[str] = Field(
        None,
        description="URL to view the pipeline execution logs (if available)"
    )

    error_message: Optional[str] = Field(
        None,
        description="Error message if the trigger failed"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "status": "success",
                "message": "Pipeline run triggered successfully",
                "run_code": "pipeline_payment_prod_main_run_1730793600",
                "run_status": "PENDING",
                "commit_sha": "a1b2c3d4e5f6g7h8i9j0",
                "commit_url": "https://github.com/owner/repo/commit/a1b2c3d4e5f6g7h8i9j0",
                "log_url": None,
                "error_message": None
            }
        }


class GetPipelinesByServiceRequest(BaseModel):
    """Request schema for fetching pipelines by source entity"""
    transaction_code: str = Field(
        ...,
        description="Source entity code (service_config.code or infrastructure_mst.code)",
        min_length=1,
        max_length=100
    )
    table_name: str = Field(
        default="SERVICE_CONFIG",
        description="Source table discriminator (SERVICE_CONFIG, INFRASTRUCTURE)",
        max_length=50
    )
    skip: int = Field(
        default=0,
        ge=0,
        description="Number of records to skip for pagination"
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Maximum number of records to return"
    )
    geo_loc_mst_code: Optional[str] = Field(
        None,
        description="Filter by geographic location code (e.g., 'mumbai', 'london')",
        max_length=100
    )

    @field_validator("transaction_code")
    @classmethod
    def validate_transaction_code(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("transaction_code cannot be empty or whitespace")
        return v.strip()


class PipelineDetailItem(BaseModel):
    """Individual pipeline detail item"""
    transaction_code: str = Field(..., description="Source entity code (service_config.code or infrastructure_mst.code)")
    table_name: str = Field(..., description="Source table discriminator (SERVICE_CONFIG, INFRASTRUCTURE, etc.)")
    created_at: datetime = Field(..., description="Pipeline creation timestamp")
    name: str = Field(..., description="Pipeline name")
    code: str = Field(..., description="Pipeline code")
    description: Optional[str] = Field(None, description="Pipeline description")
    repo_url: str = Field(..., description="Repository URL")
    repo_branch: str = Field(..., description="Repository branch")
    geo_loc_mst_code: Optional[str] = Field(None, description="Geographic location code (e.g., 'mumbai', 'london')")
    pipeline_agent: Optional[str] = Field(None, description="Pipeline agent type (e.g., GitHub Actions, Jenkins)")
    language: Optional[str] = Field(None, description="Programming language name")
    last_deploy_time: Optional[datetime] = Field(None, description="Timestamp of the most recent pipeline run")
    last_deploy_status: Optional[str] = Field(None, description="Status of the most recent pipeline run (PENDING, RUNNING, COMPLETED, FAILED, etc.)")
    gitops_workflow: Optional[GitopsWorkflowDetailInfo] = Field(None, description="GitOps workflow details (PR info, commit SHA, etc.)")
    # Whether the CALLER may trigger this pipeline — can_deploy on the service
    # it belongs to. A UI hint so the Trigger button can be disabled up front
    # rather than pressed and refused; POST /run enforces the same check
    # regardless of what this says. Defaults open so any older producer of
    # this shape keeps rendering; the one producer sets it explicitly.
    can_deploy: bool = Field(True, description="Caller holds can_deploy on the owning service — may trigger this pipeline")

    class Config:
        from_attributes = True


class GetPipelinesByServiceResponse(BaseModel):
    """Response schema for fetching pipelines by service"""
    total: int = Field(..., description="Total number of pipelines for the service")
    skip: int = Field(..., description="Number of records skipped")
    limit: int = Field(..., description="Maximum number of records returned")
    pipelines: List[PipelineDetailItem] = Field(default=[], description="List of pipeline details")


class GetPipelineRunHistoryRequest(BaseModel):
    """Request schema for fetching pipeline run history"""
    pipeline_mst_code: Optional[str] = Field(
        None,
        description="Pipeline code to filter runs (if not provided, returns all runs)",
        min_length=1,
        max_length=100
    )
    skip: int = Field(
        default=0,
        ge=0,
        description="Number of records to skip for pagination"
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Maximum number of records to return"
    )
    resource_transaction_code: Optional[str] = Field(
        None,
        description=(
            "Optional: when paired with resource_table_name, restricts runs to "
            "those whose transaction_queue_code JSONB array contains a queue "
            "item pointing at this source entity. Used to narrow shared "
            "infra-apply pipeline runs to a specific infra resource."
        ),
        max_length=100,
    )
    resource_table_name: Optional[str] = Field(
        None,
        description="Source table discriminator paired with resource_transaction_code (e.g. 'INFRASTRUCTURE').",
        max_length=50,
    )

    @field_validator("pipeline_mst_code")
    @classmethod
    def validate_pipeline_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        return v


class PipelineRunHistoryItem(BaseModel):
    """Individual pipeline run history item"""
    id: int = Field(..., description="Run ID")
    pipeline_mst_code: str = Field(..., description="Pipeline code")
    code: str = Field(..., description="Unique run code")
    status: str = Field(..., description="Run status (PENDING, RUNNING, COMPLETED, FAILED, etc.)")
    log_url: Optional[str] = Field(None, description="URL to view the pipeline execution logs")
    created_at: datetime = Field(..., description="Run creation timestamp")
    updated_at: Optional[datetime] = Field(None, description="Last update timestamp")
    commit_sha: Optional[str] = Field(None, description="GitHub commit SHA that triggered the run")
    github_run_id: Optional[str] = Field(None, description="GitHub Actions run ID")
    error_message: Optional[str] = Field(None, description="Error message if run failed")
    build_number: Optional[int] = Field(None, description="Jenkins build number")
    build_stages: Optional[List[Dict[str, Any]]] = Field(None, description="Stage breakdown [{name, status, duration_secs, started_at}]")
    deploy_result: Optional[Dict[str, Any]] = Field(None, description="Deploy result: {alb_url, build_url, build_result, completed_at}")

    class Config:
        from_attributes = True


class GetPipelineRunHistoryResponse(BaseModel):
    """Response schema for fetching pipeline run history"""
    total: int = Field(..., description="Total number of pipeline runs")
    skip: int = Field(..., description="Number of records skipped")
    limit: int = Field(..., description="Maximum number of records returned")
    runs: List[PipelineRunHistoryItem] = Field(default=[], description="List of pipeline run history")


# ============================================================================
# Deployment Pipeline Schemas (Simple YAML Generation)
# ============================================================================

class DeploymentPipelineCreateRequest(BaseModel):
    """Request schema for creating a deployment pipeline (simple YAML generation)"""

    pipeline_name: str = Field(
        ...,
        description="Name for the workflow file (e.g., deploy-order-service)",
        min_length=1,
        max_length=100
    )

    github_repository: str = Field(
        ...,
        description="Target GitHub repository (e.g., Regobs/terraform-test)",
        min_length=1,
        max_length=500
    )

    branch_name: str = Field(
        ...,
        description="Branch to trigger on (e.g., main, develop)",
        min_length=1,
        max_length=100
    )

    folder_path: Optional[str] = Field(
        None,
        description="Optional subfolder path for monorepo support",
        max_length=500
    )

    services_mst_code: str = Field(
        ...,
        description="Service code to fetch related data from database",
        min_length=1,
        max_length=100
    )

    environment: EnvironmentEnum = Field(
        ...,
        description="Deployment environment (dev, staging, prod)"
    )

    @field_validator("pipeline_name")
    @classmethod
    def validate_pipeline_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("pipeline_name cannot be empty or whitespace")
        # Sanitize for use as filename
        return v.strip().lower().replace(" ", "-")

    @field_validator("github_repository")
    @classmethod
    def validate_github_repository(cls, v: str) -> str:
        v = v.strip()
        if not v or "/" not in v:
            raise ValueError("github_repository must be in format: owner/repo")
        return v

    @field_validator("branch_name")
    @classmethod
    def validate_branch_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("branch_name cannot be empty or whitespace")
        return v.strip()

    @field_validator("folder_path")
    @classmethod
    def validate_folder_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "pipeline_name": "deploy-order-service",
                "github_repository": "Regobs/terraform-test",
                "branch_name": "main",
                "folder_path": "services/order-service",
                "services_mst_code": "19960788-c18a-4669-af13-bee85b6a1765",
                "environment": "dev"
            }
        }


class DeploymentPipelineCreateResponse(BaseModel):
    """Response schema for deployment pipeline creation"""

    status: str = Field(
        ...,
        description="Operation status (success/error)"
    )

    message: str = Field(
        ...,
        description="Human-readable message describing the result"
    )

    yaml_content: Optional[str] = Field(
        None,
        description="Generated GitHub Actions workflow YAML content"
    )

    github_commit_sha: Optional[str] = Field(
        None,
        description="GitHub commit SHA after pushing workflow file"
    )

    workflow_file_path: Optional[str] = Field(
        None,
        description="Path to the workflow file in the repository"
    )

    github_commit_url: Optional[str] = Field(
        None,
        description="Direct URL to view the GitHub commit"
    )

    ecr_repository: Optional[str] = Field(
        None,
        description="ECR repository URL for the service"
    )

    ecs_service_name: Optional[str] = Field(
        None,
        description="ECS service name"
    )

    cluster_name: Optional[str] = Field(
        None,
        description="ECS cluster name"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "status": "success",
                "message": "Deployment pipeline created successfully for order-service",
                "yaml_content": "name: Deploy order-service to dev\n...",
                "github_commit_sha": "a1b2c3d4e5f6",
                "workflow_file_path": ".github/workflows/deploy-order-service.yml",
                "github_commit_url": "https://github.com/Regobs/terraform-test/commit/a1b2c3d4e5f6",
                "ecr_repository": "597189966628.dkr.ecr.ap-south-1.amazonaws.com/app-dev-order-service",
                "ecs_service_name": "app-dev-apsouth1-01-order-service-svc",
                "cluster_name": "aslam_ecs_ec2"
            }
        }


# ============================================================================
# Add Workflow Dispatch Schemas
# ============================================================================

class AddWorkflowDispatchRequest(BaseModel):
    """Request schema for adding workflow_dispatch trigger to a pipeline"""

    pipeline_code: str = Field(
        ...,
        description="Pipeline code to update",
        min_length=1,
        max_length=100
    )

    @field_validator("pipeline_code")
    @classmethod
    def validate_pipeline_code(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("pipeline_code cannot be empty or whitespace")
        return v.strip()

    class Config:
        json_schema_extra = {
            "example": {
                "pipeline_code": "pipeline_payment_prod_main"
            }
        }


class AddWorkflowDispatchResponse(BaseModel):
    """Response schema for adding workflow_dispatch trigger"""

    status: str = Field(
        ...,
        description="Status of the operation: success, already_exists, or error"
    )

    message: str = Field(
        ...,
        description="Human-readable message describing the result"
    )

    pipeline_code: str = Field(
        ...,
        description="Pipeline code that was processed"
    )

    pr_url: Optional[str] = Field(
        None,
        description="URL of the created/updated PR"
    )

    pr_number: Optional[int] = Field(
        None,
        description="PR number"
    )

    commit_sha: Optional[str] = Field(
        None,
        description="Commit SHA of the change"
    )

    feature_branch: Optional[str] = Field(
        None,
        description="Feature branch name"
    )

    gitops_workflow_id: Optional[int] = Field(
        None,
        description="GitOps workflow tracking ID"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "status": "success",
                "message": "Created PR #42 to add workflow_dispatch trigger",
                "pipeline_code": "pipeline_payment_prod_main",
                "pr_url": "https://github.com/owner/repo/pull/42",
                "pr_number": 42,
                "commit_sha": "a1b2c3d4e5f6g7h8i9j0",
                "feature_branch": "workflow-dispatch/payment-service-prod-1",
                "gitops_workflow_id": 123
            }
        }


# ============================================================================
# Build Logs & Status Schemas
# ============================================================================

class BuildLogLineItem(BaseModel):
    """Single parsed log line."""
    message: str = Field(..., description="Log line text")
    timestamp: Optional[str] = Field(None, description="Timestamp if present")
    step_num: Optional[str] = Field(None, description="Docker step number (e.g. '2/6')")
    command: Optional[str] = Field(None, description="Docker command (e.g. 'RUN apt-get update')")
    status: Optional[str] = Field(None, description="Step status: done, cached, running")
    duration: Optional[str] = Field(None, description="Step duration (e.g. '0.4s')")
    docker_stage: Optional[str] = Field(None, description="Docker build stage name (e.g. 'build', 'stage-1') for multi-stage builds")
    sub_lines: Optional[List[str]] = Field(None, description="Sub-output lines for Docker steps")


class BuildLogStageItem(BaseModel):
    """Parsed stage section from build log."""
    name: str = Field(..., description="Stage name")
    status: str = Field(default="success", description="Stage status: success, failed, skipped")
    lines: List[BuildLogLineItem] = Field(default=[], description="Parsed log lines")


class BuildLogsResponse(BaseModel):
    """Response for progressive build log fetching."""
    sections: List[BuildLogStageItem] = Field(default=[], description="Parsed log sections by stage")
    next_offset: int = Field(..., description="Byte offset for next poll")
    has_more: bool = Field(..., description="Whether more log data is expected")
    build_status: Optional[str] = Field(None, description="Current build status (building, SUCCESS, FAILURE, etc.)")


class BuildStageInfo(BaseModel):
    """Stage info from pipeline_run_track.build_stages."""
    name: str = Field(..., description="Stage name")
    status: str = Field(..., description="Stage status: running, success, failed")
    started_at: Optional[str] = Field(None, description="ISO timestamp when stage started")
    duration_secs: Optional[int] = Field(None, description="Stage duration in seconds")


class BuildStatusResponse(BaseModel):
    """Response for latest build status."""
    pipeline_code: str = Field(..., description="Pipeline code")
    build_number: Optional[int] = Field(None, description="Jenkins build number")
    run_code: Optional[str] = Field(None, description="Pipeline run track code")
    status: str = Field(..., description="Run status (PENDING, RUNNING, COMPLETED, FAILED, etc.)")
    current_stage: Optional[str] = Field(None, description="Currently running stage name")
    started_at: Optional[datetime] = Field(None, description="Build start time")
    stages: List[BuildStageInfo] = Field(default=[], description="Stage breakdown")
    log_url: Optional[str] = Field(None, description="Jenkins build URL")
    commit_sha: Optional[str] = Field(None, description="Git commit SHA")
