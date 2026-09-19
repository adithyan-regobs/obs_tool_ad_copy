"""
Pipeline Management API Endpoints

Handles CI/CD pipeline creation and management operations.
"""

from typing import Tuple, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.config import settings
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.pipeline_mst_model import PipelineMstModel
from app.db.models.pipeline_run_track_model import PipelineRunTrackModel
from app.core.enum import PipelineRunStatusEnum, WorkflowSourceTableEnum
from app.core.authz import fga
from app.core.authz.objects import config_object
from app.core.authz.security import authenticated_user, require
from app.integrations.jenkins_integration import JenkinsIntegration
from app.schemas.pipeline_schemas import (
    CreatePipelineRequest,
    CreatePipelineResponse,
    SavePipelineRequest,
    SavePipelineResponse,
    RunPipelineRequest,
    RunPipelineResponse,
    GetPipelinesByServiceRequest,
    GetPipelinesByServiceResponse,
    GetPipelineRunHistoryRequest,
    GetPipelineRunHistoryResponse,
    AddWorkflowDispatchRequest,
    AddWorkflowDispatchResponse,
    BuildLogsResponse,
    BuildStatusResponse,
    BuildStageInfo,
    BuildLogStageItem,
    BuildLogLineItem,
)
from app.services.pipeline_mgmt_service import PipelineMgmtService
from app.services.jenkins_log_parser import parse_jenkins_log

logger = logging.getLogger(__name__)

router = APIRouter()


def _is_service_pipeline(table_name) -> bool:
    """Pipelines belong to a service_config row or an infrastructure row. Only
    the service kind has an FGA object to check against — `service:<code>` in
    rbac-model.fga carries can_deploy; nothing in the model describes an
    infrastructure row yet, so those pipelines stay login-only here rather
    than being refused for everyone by a check against an object that does
    not exist."""
    value = table_name.value if hasattr(table_name, "value") else str(table_name or "")
    return value.upper() == WorkflowSourceTableEnum.SERVICE_CONFIG.value.upper()


@router.post(
    "/create",
    summary="Create Pipeline",
    response_model=CreatePipelineResponse,
    response_model_exclude_none=True,
    status_code=201
)
async def create_pipeline(
    data: CreatePipelineRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> CreatePipelineResponse:
    """
    Create a new CI/CD pipeline with AWS resources and YAML generation.

    Security:
        - JWT authentication required

    This endpoint performs the following operations:
    1. Validates input and checks for duplicate pipelines
    2. Lookups service and pipeline vendor configuration (hierarchical)
    3. Creates AWS ECR repository for container images
    4. Creates GitHub OIDC IAM role with necessary permissions
    5. Generates GitHub Actions workflow YAML from template
    6. Saves pipeline configuration to database

    **Requirements:**
    - Service must exist and use AWS infrastructure
    - Pipeline vendor must be configured (service → resource group → application → tenant)
    - Language reference must exist and support GitHub Actions
    - AWS vendor account must be configured with valid credentials

    **AWS Resources Created:**
    - ECR Repository: `{tenant}_{region}_{env}_{service}`
    - IAM Role: `GitHubActions_{service_code}_{repo}_{env}`
    - GitHub OIDC Provider (if not exists)

    **Returns:**
    - Pipeline code for reference
    - ECR repository URL for pushing images
    - IAM role ARN for GitHub Actions
    - Generated YAML workflow content

    **Errors:**
    - 400: Validation error or duplicate pipeline
    - 404: Service, language, or vendor configuration not found
    - 500: AWS operation failed or template not found
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant


        service = PipelineMgmtService(db)
        result = await service.create_pipeline(data, tenant_code=tenant.code, user_code=user.code)
        return result
    except HTTPException:
        # Re-raise HTTP exceptions from service layer
        raise
    except ValueError as e:
        # Business logic validation errors
        logger.error(f"Validation error in create_pipeline: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Unexpected errors
        logger.error(f"Unexpected error in create_pipeline: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while creating the pipeline"
        )


@router.post(
    "/save",
    summary="Save Pipeline (Onboarding)",
    response_model=SavePipelineResponse,
    response_model_exclude_none=True,
    status_code=201
)
async def save_pipeline(
    data: SavePipelineRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> SavePipelineResponse:
    """
    Save pipeline configuration for existing client pipelines (onboarding).

    Security:
        - JWT authentication required

    This endpoint is for onboarding existing pipelines without GitHub operations.
    No PR, branch, or commit is created - only the database record is saved.

    **Use Case:**
    - Onboarding existing client pipelines that already have workflow files
    - No GitHub operations (no commit, no PR, no branch creation)
    - Workflow file path is provided by frontend (existing file)

    **Operations Performed:**
    1. Validates input and checks for duplicate pipelines
    2. Lookups service and validates infrastructure
    3. Lookups geo location, service config, language reference
    4. Hierarchical pipeline vendor lookup
    5. Hierarchical infrastructure lookup
    6. Constructs ECR repo URL and IAM role ARN (for reference)
    7. Saves pipeline to database with workflow_file_path

    **Operations Skipped (vs /create):**
    - No YAML generation
    - No GitHub commit/PR/branch creation
    - No GitOps workflow tracking
    - No initial pipeline run tracking

    **Requirements:**
    - Service must exist and use AWS infrastructure
    - Pipeline vendor must be configured
    - Service config must exist for the environment and geo location
    - Infrastructure must be configured

    **Returns:**
    - Pipeline code for reference
    - ECR repository URL (pre-existing, managed by Terraform)
    - IAM role ARN (pre-existing, managed by Terraform)
    - Workflow file path (as provided)

    **Errors:**
    - 400: Validation error or duplicate pipeline
    - 404: Service, geo location, config, or infrastructure not found
    - 500: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        service = PipelineMgmtService(db)
        result = await service.save_pipeline(data, tenant_code=tenant.code, user_code=user.code)
        return result
    except HTTPException:
        # Re-raise HTTP exceptions from service layer
        raise
    except ValueError as e:
        # Business logic validation errors
        logger.error(f"Validation error in save_pipeline: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Unexpected errors
        logger.error(f"Unexpected error in save_pipeline: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while saving the pipeline"
        )


@router.post(
    "/run",
    summary="Run Pipeline",
    response_model=RunPipelineResponse,
    response_model_exclude_none=True,
    status_code=200
)
async def run_pipeline(
    data: RunPipelineRequest,
    request: Request,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    fga_user: str = Depends(authenticated_user),
    db: AsyncSession = Depends(get_db)
) -> RunPipelineResponse:
    """
    Trigger a pipeline run via empty commit.

    Security:
        - JWT authentication required
        - can_deploy on the pipeline's own service (checked below). A run
          rebuilds, pushes to ECR and restarts the service using DevLift's
          GitHub App token, so it acts for people whose own GitHub access
          would not allow it — the same permission that gates a deploy gates
          this. Checked in the handler rather than as a route card: the body
          names a PIPELINE, and the object to check is the service that
          pipeline belongs to, which only the row can say. A body-field card
          would let the caller name any service alongside any pipeline.

    This endpoint performs the following operations:
    1. Validates that the pipeline exists and is active
    2. Creates a PipelineRunTrack record with PENDING status
    3. Creates an empty commit on the pipeline's branch
    4. The empty commit triggers the GitHub Actions workflow
    5. Updates the run track with commit SHA

    **How it works:**
    - Creates an empty commit (same tree as parent, new commit message)
    - The commit message includes a timestamp
    - GitHub Actions detects the new commit and runs the workflow
    - The run track record is created BEFORE the commit for tracking

    **Requirements:**
    - Pipeline must exist and be active
    - GitHub token must be configured with 'workflow' scope
    - Repository and branch must exist

    **Returns:**
    - Run tracking code for reference
    - Commit SHA and URL
    - Initial run status (PENDING)
    - Error message if trigger failed

    **Errors:**
    - 400: Pipeline is not active
    - 404: Pipeline not found
    - 500: GitHub API error or commit failed
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Resolve the pipeline FIRST so the check runs against ITS service.
        # Same lookup and 404 the service layer does a moment later; done here
        # because the permission has to be settled before anything is
        # triggered, and the service layer has no request to mark.
        from app.repository.pipeline_mst_repository import PipelineMstRepository
        pipeline = await PipelineMstRepository(db).get_by_code(data.pipeline_code)
        if pipeline is None:
            raise HTTPException(status_code=404, detail=f"Pipeline not found: {data.pipeline_code}")
        if _is_service_pipeline(pipeline.table_name):
            await require(
                request, fga_user, "can_deploy",
                config_object(pipeline.transaction_code), 403,
                "You don't have permission to run this pipeline — it needs "
                "deploy access on the service it belongs to.",
            )

        service = PipelineMgmtService(db)
        result = await service.run_pipeline(
            pipeline_code=data.pipeline_code,
            user_code=user.code,
            user_email=user.email_id
        )
        return RunPipelineResponse(**result)
    except HTTPException:
        # Re-raise HTTP exceptions from service layer
        raise
    except ValueError as e:
        # Business logic validation errors
        logger.error(f"Validation error in run_pipeline: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Unexpected errors
        logger.error(f"Unexpected error in run_pipeline: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while running the pipeline"
        )


@router.post(
    "/get-pipelines-by-service",
    summary="Get Pipelines by Service",
    response_model=GetPipelinesByServiceResponse,
    status_code=200
)
async def get_pipelines_by_service(
    data: GetPipelinesByServiceRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    fga_user: str = Depends(authenticated_user),
    db: AsyncSession = Depends(get_db)
) -> GetPipelinesByServiceResponse:
    """
    Fetch all pipelines for a specific service with pagination.

    Security:
        - JWT authentication required

    This endpoint retrieves all pipelines associated with a service and includes
    related data from joined tables:
    - Pipeline vendor information (pipeline agent type)
    - Language reference information (programming language name)

    **Requirements:**
    - Valid service_mst_code

    **Returns:**
    - Total count of pipelines for the service
    - Paginated list of pipeline details including:
      - Environment (dev, staging, prod)
      - Creation timestamp
      - Pipeline name and description
      - Repository URL and branch
      - Folder path (if specified)
      - Pipeline agent type (e.g., GitHub Actions)
      - Programming language

    **Errors:**
    - 400: Validation error (invalid service code)
    - 500: Database error or unexpected failure
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        service = PipelineMgmtService(db)
        result = await service.get_pipelines_by_service(
            transaction_code=data.transaction_code,
            table_name=data.table_name,
            skip=data.skip,
            limit=data.limit,
            geo_loc_mst_code=data.geo_loc_mst_code,
        )

        # Stamp each pipeline with whether THIS caller may trigger it, so the
        # UI can disable the button instead of offering one that 403s. Every
        # pipeline on the page belongs to the one service the request names,
        # so it is a single check, not one per row. Infrastructure pipelines
        # have no FGA object (see _is_service_pipeline) and stay open here
        # exactly as POST /run leaves them.
        can_deploy = True
        if _is_service_pipeline(data.table_name):
            can_deploy = await fga.check(
                fga_user, "can_deploy", config_object(data.transaction_code)
            )
        pipelines = [{**p, "can_deploy": can_deploy} for p in result["pipelines"]]

        return GetPipelinesByServiceResponse(
            total=result["total"],
            skip=data.skip,
            limit=data.limit,
            pipelines=pipelines
        )
    except HTTPException:
        # Re-raise HTTP exceptions from service layer
        raise
    except ValueError as e:
        # Business logic validation errors
        logger.error(f"Validation error in get_pipelines_by_service: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Unexpected errors
        logger.error(f"Unexpected error in get_pipelines_by_service: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while fetching pipelines"
        )


@router.post(
    "/get-run-history",
    summary="Get Pipeline Run History",
    response_model=GetPipelineRunHistoryResponse,
    status_code=200
)
async def get_pipeline_run_history(
    data: GetPipelineRunHistoryRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> GetPipelineRunHistoryResponse:
    """
    Fetch pipeline run history with optional filtering by pipeline code.

    Security:
        - JWT authentication required

    This endpoint retrieves the execution history of pipeline runs from the
    `pipeline_run_track` table. It supports:
    - Filtering by specific pipeline code
    - Fetching all pipeline runs (when pipeline_mst_code is not provided)
    - Pagination for large result sets

    **Query Parameters:**
    - `pipeline_mst_code` (optional): Filter runs for a specific pipeline
      - If provided: Returns runs for that pipeline only
      - If omitted/null: Returns all pipeline runs across all pipelines
    - `skip` (default: 0): Number of records to skip for pagination
    - `limit` (default: 100, max: 500): Maximum number of records to return

    **Returns:**
    - Total count of runs matching the filter
    - Paginated list of run history including:
      - Run ID and unique run code
      - Pipeline code that was executed
      - Current status (PENDING, RUNNING, COMPLETED, FAILED, etc.)
      - Log URL for viewing execution details
      - Timestamps (created_at, updated_at)
      - GitHub commit SHA and run ID
      - Error message (if run failed)

    **Use Cases:**
    - View all runs for a specific pipeline: Set `pipeline_mst_code`
    - Monitor all pipeline executions: Omit `pipeline_mst_code`
    - Paginate through large history: Use `skip` and `limit`

    **Errors:**
    - 400: Validation error (invalid pipeline code format)
    - 500: Database error or unexpected failure
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        service = PipelineMgmtService(db)
        result = await service.get_pipeline_run_history(
            pipeline_mst_code=data.pipeline_mst_code,
            skip=data.skip,
            limit=data.limit,
            resource_transaction_code=data.resource_transaction_code,
            resource_table_name=data.resource_table_name,
        )

        return GetPipelineRunHistoryResponse(
            total=result["total"],
            skip=data.skip,
            limit=data.limit,
            runs=result["runs"]
        )
    except HTTPException:
        # Re-raise HTTP exceptions from service layer
        raise
    except ValueError as e:
        # Business logic validation errors
        logger.error(f"Validation error in get_pipeline_run_history: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Unexpected errors
        logger.error(f"Unexpected error in get_pipeline_run_history: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while fetching pipeline run history"
        )


@router.post(
    "/add-workflow-dispatch",
    summary="Add Workflow Dispatch Trigger",
    response_model=AddWorkflowDispatchResponse,
    response_model_exclude_none=True,
    status_code=200
)
async def add_workflow_dispatch(
    data: AddWorkflowDispatchRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> AddWorkflowDispatchResponse:
    """
    Add workflow_dispatch trigger to an existing pipeline's workflow file.

    Security:
        - JWT authentication required

    This endpoint enables manual triggering of a pipeline via GitHub Actions UI
    by adding the `workflow_dispatch:` trigger to the workflow YAML file.

    **Operations Performed:**
    1. Fetches the pipeline configuration from the database
    2. Retrieves the workflow YAML file from the GitHub repository
    3. Checks if `workflow_dispatch:` trigger already exists
    4. Inserts `workflow_dispatch:` into the `on:` block
    5. Creates a versioned feature branch (e.g., workflow-dispatch/service-dev-1)
    6. Commits the updated workflow file
    7. Creates a PR to the base branch

    **Branch Naming Convention:**
    - Format: `workflow-dispatch/{service-name}-{environment}-{version}`
    - Version is auto-incremented based on existing branches

    **Requirements:**
    - Pipeline must exist with a configured workflow_file_path
    - GitHub token must be available via pipeline vendor configuration
    - Workflow file must exist in the repository

    **Returns:**
    - Status: success, already_exists, or error
    - PR URL and number (if created)
    - Commit SHA and feature branch name

    **Errors:**
    - 400: Validation error (invalid pipeline code)
    - 404: Pipeline not found
    - 500: GitHub API error or unexpected failure
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        logger.info(f"Add workflow_dispatch request - User: {user.code}, Tenant: {tenant.code}, Pipeline: {data.pipeline_code}")

        service = PipelineMgmtService(db)
        result = await service.add_workflow_dispatch(
            pipeline_code=data.pipeline_code,
            tenant_code=tenant.code,
            user_code=user.code
        )

        return AddWorkflowDispatchResponse(**result)
    except HTTPException:
        # Re-raise HTTP exceptions from service layer
        raise
    except ValueError as e:
        # Business logic validation errors
        logger.error(f"Validation error in add_workflow_dispatch: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Unexpected errors
        logger.error(f"Unexpected error in add_workflow_dispatch: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while adding workflow_dispatch trigger"
        )


# =============================================================================
# Build Logs & Status Endpoints
# =============================================================================


async def _get_jenkins_client_for_pipeline(
    pipeline: PipelineMstModel,
) -> JenkinsIntegration:
    """Build a JenkinsIntegration client from env settings."""
    jenkins_url = settings.jenkins_url
    jenkins_user = settings.jenkins_user
    jenkins_api_token = settings.jenkins_api_token
    if not all([jenkins_url, jenkins_user, jenkins_api_token]):
        raise HTTPException(
            status_code=500,
            detail="Jenkins credentials not configured on server",
        )
    return JenkinsIntegration(jenkins_url, jenkins_user, jenkins_api_token)


async def _get_pipeline_by_code(
    db: AsyncSession, pipeline_code: str
) -> PipelineMstModel:
    """Fetch a pipeline_mst by code, raise 404 if not found."""
    stmt = select(PipelineMstModel).where(
        and_(
            PipelineMstModel.code == pipeline_code,
            PipelineMstModel.is_deleted == False,
        )
    )
    result = await db.execute(stmt)
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    return pipeline


@router.get(
    "/{pipeline_code}/builds/{build_number}/logs",
    summary="Get Build Logs (Progressive)",
    response_model=BuildLogsResponse,
    status_code=200,
)
async def get_build_logs(
    pipeline_code: str,
    build_number: int,
    start: int = Query(default=0, ge=0, description="Byte offset for progressive fetch"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> BuildLogsResponse:
    """
    Fetch build logs progressively from Jenkins and return parsed sections.

    Uses Jenkins progressive text API — frontend polls every 2-3s with
    `start=next_offset` while `has_more=true`.
    """
    try:
        pipeline = await _get_pipeline_by_code(db, pipeline_code)
        deployment_config = pipeline.deployment_config or {}
        job_name = deployment_config.get("jenkins_job_name")
        if not job_name:
            raise HTTPException(
                status_code=400,
                detail="Pipeline has no Jenkins job configured",
            )

        jenkins = await _get_jenkins_client_for_pipeline(pipeline)

        # Fetch progressive log from Jenkins
        log_result = await jenkins.get_build_console_log_progressive(
            job_name, build_number, start=start
        )
        raw_text = log_result.get("text", "")
        next_offset = log_result.get("next_offset", start)
        has_more = log_result.get("has_more", False)

        # Get build status
        build_status_result = await jenkins.get_build_status(job_name, build_number)
        build_status: Optional[str] = None
        if build_status_result.get("status") == "success":
            if build_status_result.get("building"):
                build_status = "building"
            else:
                build_status = build_status_result.get("result", "UNKNOWN")

        # Parse log text into structured sections
        parsed = parse_jenkins_log(raw_text) if raw_text.strip() else {"stages": []}

        sections = []
        for stage_data in parsed["stages"]:
            # Skip stages that were skipped by Jenkins (e.g. Checkout/Build for model serving)
            if stage_data.get("status") == "skipped":
                continue
            lines = []
            for line_data in stage_data.get("lines", []):
                lines.append(BuildLogLineItem(
                    message=line_data["message"],
                    timestamp=line_data.get("timestamp"),
                    step_num=line_data.get("step_num"),
                    command=line_data.get("command"),
                    status=line_data.get("status"),
                    duration=line_data.get("duration"),
                    sub_lines=line_data.get("sub_lines") or None,
                ))
            sections.append(BuildLogStageItem(
                name=stage_data["name"],
                status=stage_data.get("status", "success"),
                lines=lines,
            ))

        return BuildLogsResponse(
            sections=sections,
            next_offset=next_offset,
            has_more=has_more,
            build_status=build_status,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching build logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch build logs")


@router.get(
    "/{pipeline_code}/builds/latest/status",
    summary="Get Latest Build Status",
    response_model=BuildStatusResponse,
    status_code=200,
)
async def get_latest_build_status(
    pipeline_code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> BuildStatusResponse:
    """
    Get the latest build status for a pipeline, including current stage and stage breakdown.
    """
    try:
        pipeline = await _get_pipeline_by_code(db, pipeline_code)

        # Find latest run_track for this pipeline
        stmt = (
            select(PipelineRunTrackModel)
            .where(
                and_(
                    PipelineRunTrackModel.pipeline_mst_code == pipeline_code,
                    PipelineRunTrackModel.is_deleted == False,
                )
            )
            .order_by(PipelineRunTrackModel.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        run_track = result.scalar_one_or_none()

        if not run_track:
            return BuildStatusResponse(
                pipeline_code=pipeline_code,
                status="NO_BUILDS",
            )

        # Extract stage info
        stages_data = run_track.build_stages or []
        stages = [
            BuildStageInfo(
                name=s.get("name", "unknown"),
                status=s.get("status", "unknown"),
                started_at=s.get("started_at"),
                duration_secs=s.get("duration_secs"),
            )
            for s in stages_data
        ]

        # Determine current stage
        current_stage = None
        for s in stages_data:
            if s.get("status") == "running":
                current_stage = s.get("name")
                break

        return BuildStatusResponse(
            pipeline_code=pipeline_code,
            build_number=run_track.build_number,
            run_code=run_track.code,
            status=run_track.status.value if hasattr(run_track.status, "value") else str(run_track.status),
            current_stage=current_stage,
            started_at=run_track.created_at,
            stages=stages,
            log_url=run_track.log_url,
            commit_sha=run_track.commit_sha,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching build status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch build status")


# =============================================================================
# DEPRECATED ENDPOINT - Use /create instead
# =============================================================================
# @router.post(
#     "/deployment-pipeline/create",
#     summary="Create Deployment Pipeline (Simple)",
#     response_model=DeploymentPipelineCreateResponse,
#     response_model_exclude_none=True,
#     status_code=201
# )
# async def create_deployment_pipeline(
#     data: DeploymentPipelineCreateRequest,
#     user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
#     db: AsyncSession = Depends(get_db)
# ) -> DeploymentPipelineCreateResponse:
#     """
#     DEPRECATED: Use /create endpoint with PipelineMgmtService instead.
#     """
#     raise HTTPException(
#         status_code=410,
#         detail="This endpoint is deprecated. Use POST /pipelines/create instead."
#     )
