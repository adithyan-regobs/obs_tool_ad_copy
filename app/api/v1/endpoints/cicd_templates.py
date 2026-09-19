"""
API endpoints for CI/CD template operations.
Provides endpoints to manage CI/CD workflow templates.
"""
import uuid
from typing import Tuple, Optional, List
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, or_, and_

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.cicd_template_ref_model import CicdTemplateRefModel
from app.db.models.services_mst_model import ServicesMstModel
from app.schemas.cicd_template_schemas import (
    CicdTemplateCreate,
    CicdTemplateUpdate,
    CicdTemplateResponse,
    CicdTemplateListResponse,
    WorkflowInstanceCreate,
    WorkflowInstanceResponse
)
from app.core.enum import PipelineAgentEnum
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()


@router.get("/pipelines", summary="List Saved Workflows (Pipelines)")
async def list_pipelines(
    transaction_code: Optional[str] = Query(None, description="Filter by transaction code (service_config.code or infrastructure_mst.code)"),
    table_name: Optional[str] = Query(None, description="Filter by table name discriminator (SERVICE_CONFIG, INFRASTRUCTURE)"),
    geo_loc_code: Optional[str] = Query(None, description="Filter by geo location code"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    List saved workflows (pipelines) from pipeline_mst table.

    Returns all pipeline records for the tenant's services, optionally filtered by various parameters.

    **Query Parameters:**
    - `transaction_code`: Optional filter for transaction code (service_config.code)
    - `geo_loc_code`: Optional filter for geo location

    **Response:**
    ```json
    {
        "pipelines": [
            {
                "id": 1,
                "code": "pipeline-service-dev-abc123",
                "name": "swift-workflow-abc123",
                "transaction_code": "svc-config-123",
                "table_name": "SERVICE_CONFIG",
                "language_ref_code": "go_1_23_language_ref",
                "repo_url": "https://github.com/org/repo",
                "deployment_config": {
                    "template_code": "quick-release-template",
                    "template_name": "Quick Release",
                    "steps": [...],
                    "language": "go"
                },
                "created_at": "2025-12-25T23:51:42"
            }
        ]
    }
    ```
    """
    try:
        from app.db.models.pipeline_mst_model import PipelineMstModel
        from sqlalchemy import desc

        user, tenant = user_and_tenant

        # Build base query to fetch pipelines for tenant
        query = select(PipelineMstModel).where(
            PipelineMstModel.tenant_code == tenant.code
        )

        # Filter by transaction_code if provided
        if transaction_code:
            query = query.where(
                PipelineMstModel.transaction_code == transaction_code,
            )

        # Filter by table_name if provided
        if table_name:
            query = query.where(
                PipelineMstModel.table_name == table_name
            )

        # Filter by geo location if provided
        if geo_loc_code:
            # Filter by deployment_config JSONB field
            if geo_loc_code.lower() != "all":
                query = query.where(
                    PipelineMstModel.deployment_config['geo_loc_mst_code'].astext == geo_loc_code
                )

        # Order by creation date (newest first)
        query = query.order_by(desc(PipelineMstModel.created_at))

        result = await db.execute(query)
        pipelines = result.scalars().all()

        # Convert to response format
        pipeline_responses = []
        for pipeline in pipelines:
            pipeline_responses.append({
                "id": pipeline.id,
                "code": pipeline.code,
                "name": pipeline.name,
                "transaction_code": pipeline.transaction_code,
                "table_name": pipeline.table_name,
                "pipeline_vendor_mst_code": pipeline.pipeline_vendor_mst_code,
                "language_ref_code": pipeline.language_ref_code,
                "repo_url": pipeline.repo_url,
                "repo_branch": pipeline.repo_branch,
                "deployment_config": pipeline.deployment_config,
                "created_at": pipeline.created_at.isoformat() if pipeline.created_at else None,
                "updated_at": pipeline.updated_at.isoformat() if pipeline.updated_at else None
            })

        return {
            "pipelines": pipeline_responses,
            "total": len(pipeline_responses)
        }

    except Exception as e:
        import traceback
        print(f"Error listing pipelines: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to list pipelines: {str(e)}")


# Schema for saving workflow to pipeline
class SaveWorkflowToPipelineRequest(BaseModel):
    """Request schema for saving workflow template to pipeline"""
    template_code: str
    transaction_code: str  # service_config.code (polymorphic reference)
    pipeline_name: str  # User-provided pipeline name
    language: str
    repo_url: str
    repo_branch: str = "main"  # Deprecated: use repo_branches instead
    repo_branches: Optional[List[str]] = None  # Support for multiple branches with PR routing
    environment: str  # dev, staging, prod
    customized_steps: Optional[List[dict]] = None
    build_path: Optional[str] = None  # Path to build directory (triggers workflow)
    dockerfile_path: Optional[str] = None  # Path to Dockerfile (triggers workflow)
    additional_trigger_paths: Optional[List[str]] = None  # Additional paths that trigger the workflow
    geo_loc_mst_code: Optional[str] = None  # Geographic location master code


@router.get("", response_model=CicdTemplateListResponse, summary="List CI/CD Templates")
async def list_cicd_templates(
    service_config_code: Optional[str] = Query(None, description="Filter by service config code"),
    template_type: Optional[str] = Query(None, description="Filter by template type"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    include_system_templates: bool = Query(True, description="Include system templates"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    List all CI/CD templates for the current tenant.

    Security:
        - JWT authentication required

    **Query Parameters:**
    - `service_config_code`: (Optional) Filter templates for a specific service
    - `template_type`: (Optional) Filter by template type (e.g., 'quick_release', 'full_release', 'custom')
    - `is_active`: (Optional) Filter by active status
    - `include_system_templates`: Include predefined system templates (default: true)

    **Response:**
    ```json
    {
        "templates": [
            {
                "id": 1,
                "code": "cicd-tmpl-123",
                "name": "Quick Release",
                "description": "Fast deployment with basic checks",
                "template_type": "quick_release",
                "config": {
                    "language": "go",
                    "steps": [...]
                },
                "is_active": true,
                "is_system_template": false,
                "applications_mst_code": "svc-123",
                "tenant_mst_code": "tenant-001",
                "created_at": "2025-12-25T10:00:00Z",
                "updated_at": "2025-12-25T10:00:00Z"
            }
        ],
        "total": 1
    }
    ```
    """
    try:
        user, tenant = user_and_tenant

        # Build query - include both tenant-specific and global templates
        query = select(CicdTemplateRefModel).where(
            or_(
                CicdTemplateRefModel.tenant_mst_code == tenant.code,
                CicdTemplateRefModel.tenant_mst_code.is_(None)
            )
        )

        # Execute query
        result = await db.execute(query.order_by(CicdTemplateRefModel.created_at.desc()))
        templates = result.scalars().all()

        # Convert templates to response format, handling validation errors gracefully
        template_responses = []
        for tmpl in templates:
            try:
                response = CicdTemplateResponse.model_validate(tmpl)
                template_responses.append(response)
            except Exception as validation_error:
                # Log validation error but continue with other templates
                print(f"Validation error for template {tmpl.code}: {validation_error}")
                continue

        return CicdTemplateListResponse(
            templates=template_responses,
            total=len(template_responses)
        )

    except Exception as e:
        import traceback
        print(f"Error fetching templates: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to fetch templates: {str(e)}")


@router.get("/{template_code}", response_model=CicdTemplateResponse, summary="Get CI/CD Template by Code")
async def get_cicd_template(
    template_code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get a specific CI/CD template by code.

    Security:
        - JWT authentication required

    **Path Parameters:**
    - `template_code`: Unique template code

    **Response:**
    Returns the template details including configuration and steps.
    """
    try:
        user, tenant = user_and_tenant

        # Fetch template
        query = select(CicdTemplateRefModel).where(
            CicdTemplateRefModel.code == template_code,
            or_(
                CicdTemplateRefModel.tenant_mst_code == tenant.code,
                CicdTemplateRefModel.tenant_mst_code.is_(None)
            )
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"Template not found: {template_code}"
            )

        return CicdTemplateResponse.model_validate(template)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch template: {str(e)}")


@router.post("", response_model=CicdTemplateResponse, summary="Create CI/CD Template")
async def create_cicd_template(
    template_data: CicdTemplateCreate,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new CI/CD template.

    Security:
        - JWT authentication required

    **Request Body:**
    ```json
    {
        "name": "Quick Release",
        "description": "Fast deployment with basic checks",
        "template_type": "quick_release",
        "applications_mst_code": "svc-123",
        "config": {
            "language": "go",
            "steps": [
                {
                    "id": "code-checkout",
                    "name": "Code Checkout",
                    "order": 1,
                    "mandatory": true,
                    "enabled": true,
                    "category": "setup"
                }
            ]
        }
    }
    ```

    **Response:**
    Returns the created template with generated code and timestamps.
    """
    try:
        user, tenant = user_and_tenant

        # Workspace access guard
        if template_data.applications_mst_code:
            from app.services.workspace_service import WorkspaceService
            workspace_svc = WorkspaceService(db)
            if not await workspace_svc.verify_app_workspace_access(user.code, tenant.code, template_data.applications_mst_code):
                raise HTTPException(status_code=403, detail="Access denied: application workspace is not accessible")

        # Generate unique code using UUID
        code = f"cicd-tmpl-{str(uuid.uuid4())[:8]}"

        # Create template instance
        template = CicdTemplateRefModel(
            code=code,
            tenant_mst_code=tenant.code,
            name=template_data.name,
            description=template_data.description,
            config=template_data.config.model_dump() if hasattr(template_data.config, 'model_dump') else template_data.config,
            is_active=template_data.is_active,
            is_deleted=False,
            applications_mst_code=template_data.applications_mst_code
        )

        db.add(template)
        await db.commit()
        await db.refresh(template)

        return CicdTemplateResponse.model_validate(template)

    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create template: {str(e)}")


@router.put("/{template_code}", response_model=CicdTemplateResponse, summary="Update CI/CD Template")
async def update_cicd_template(
    template_code: str,
    template_data: CicdTemplateUpdate,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Update an existing CI/CD template.

    Security:
        - JWT authentication required

    **Path Parameters:**
    - `template_code`: Unique template code

    **Request Body:**
    All fields are optional. Only provided fields will be updated.

    **Response:**
    Returns the updated template.
    """
    try:
        user, tenant = user_and_tenant

        # Fetch template
        query = select(CicdTemplateRefModel).where(
            CicdTemplateRefModel.code == template_code,
            CicdTemplateRefModel.tenant_mst_code == tenant.code
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"Template not found: {template_code}"
            )

        # Update fields
        if template_data.name is not None:
            template.name = template_data.name
        if template_data.description is not None:
            template.description = template_data.description
        if template_data.config is not None:
            template.config = template_data.config.model_dump() if hasattr(template_data.config, 'model_dump') else template_data.config
        if template_data.is_active is not None:
            template.is_active = template_data.is_active
        if template_data.applications_mst_code is not None:
            template.applications_mst_code = template_data.applications_mst_code

        template.updated_at = datetime.utcnow()

        await db.commit()
        await db.refresh(template)

        return CicdTemplateResponse.model_validate(template)

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update template: {str(e)}")


@router.delete("/{template_code}", summary="Delete CI/CD Template")
async def delete_cicd_template(
    template_code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete a CI/CD template.

    Security:
        - JWT authentication required

    **Path Parameters:**
    - `template_code`: Unique template code

    **Response:**
    ```json
    {
        "message": "Template deleted successfully"
    }
    ```
    """
    try:
        user, tenant = user_and_tenant

        # Fetch template
        query = select(CicdTemplateRefModel).where(
            CicdTemplateRefModel.code == template_code,
            CicdTemplateRefModel.tenant_mst_code == tenant.code
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"Template not found: {template_code}"
            )

        await db.delete(template)
        await db.commit()

        return {"message": "Template deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete template: {str(e)}")


@router.post("/instances", response_model=WorkflowInstanceResponse, summary="Create Workflow Instance from Template")
async def create_workflow_instance(
    instance_data: WorkflowInstanceCreate,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a workflow instance from a template.

    This endpoint creates a concrete workflow configuration from a template,
    which can be used to generate GitHub Actions workflow files.

    **Request Body:**
    ```json
    {
        "template_id": 1,
        "service_config_code": "svc-123",
        "name": "My Service Workflow",
        "customizations": {
            "branch": "main",
            "timeout_minutes": 30
        }
    }
    ```

    **Response:**
    Returns the workflow instance with generated YAML configuration.
    """
    try:
        user, tenant = user_and_tenant

        # Fetch template
        query = select(CicdTemplateRefModel).where(
            CicdTemplateRefModel.id == instance_data.template_id,
            or_(
                CicdTemplateRefModel.tenant_mst_code == tenant.code,
                CicdTemplateRefModel.tenant_mst_code.is_(None)
            )
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"Template not found: {instance_data.template_id}"
            )

        # For now, return a simple response
        # In future, this could create a separate workflow_instances table
        from app.services.workflow_generator import generate_workflow_yaml

        # Get language from instance_data or use default
        language = instance_data.customizations.get("language", "go") if instance_data.customizations else "go"
        steps = template.config.get("steps", []) if template.config else []

        generated_yaml = generate_workflow_yaml(
            language=language,
            steps=steps
        )

        return WorkflowInstanceResponse(
            id=template.id,
            code=template.code,
            name=instance_data.name,
            service_config_code=instance_data.service_config_code,
            template_code=template.code,
            config=template.config,
            generated_workflow_yaml=generated_yaml,
            is_active=template.is_active,
            created_at=template.created_at,
            updated_at=template.updated_at
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create workflow instance: {str(e)}")


class PreviewWorkflowRequest(BaseModel):
    """Request model for workflow YAML preview."""
    language: str
    steps: List[dict]
    service_name: str = "my-service"
    environment: str = "dev"
    branches: List[str] = ["main"]
    build_path: Optional[str] = None
    dockerfile_path: Optional[str] = None
    additional_trigger_paths: Optional[List[str]] = None
    infrastructure_type: str = "eks"
    aws_region: str = "us-east-1"
    eks_cluster_name: str = "my-cluster"


class PreviewWorkflowResponse(BaseModel):
    """Response model for workflow YAML preview."""
    workflow_yaml: str
    service_name: str
    environment: str
    language: str
    branches: List[str]
    infrastructure_type: str


@router.post("/preview-workflow", summary="Preview Workflow YAML")
async def preview_workflow(
    request: PreviewWorkflowRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Generate a preview of the workflow YAML without creating a pipeline or PR.

    This endpoint generates the exact same YAML that would be created when saving
    a workflow, but without any side effects (no database writes, no GitHub operations).

    **Request Body:**
    ```json
    {
        "language": "go",
        "steps": [{"id": "build", "enabled": true, ...}],
        "service_name": "user-service",
        "environment": "dev",
        "branches": ["main", "develop"],
        "build_path": "src/api/**",
        "dockerfile_path": "Dockerfile",
        "additional_trigger_paths": ["shared/utils/**"],
        "infrastructure_type": "eks"
    }
    ```

    **Response:**
    ```json
    {
        "workflow_yaml": "name: Deploy user-service to dev\\non:\\n  ...",
        "service_name": "user-service",
        "environment": "dev",
        "language": "go",
        "branches": ["main", "develop"],
        "infrastructure_type": "eks"
    }
    ```
    """
    try:
        from app.services.workflow_generator import generate_workflow_yaml

        # Generate workflow YAML using the same function as save-to-pipeline
        workflow_yaml = generate_workflow_yaml(
            language=request.language,
            steps=request.steps,
            service_name=request.service_name,
            environment=request.environment,
            branches=request.branches,
            build_path=request.build_path,
            dockerfile_path=request.dockerfile_path,
            additional_trigger_paths=request.additional_trigger_paths,
            infrastructure_type=request.infrastructure_type,
            aws_region=request.aws_region,
            eks_cluster_name=request.eks_cluster_name
        )

        return PreviewWorkflowResponse(
            workflow_yaml=workflow_yaml,
            service_name=request.service_name,
            environment=request.environment,
            language=request.language,
            branches=request.branches,
            infrastructure_type=request.infrastructure_type
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate workflow preview: {str(e)}"
        )


@router.post("/save-to-pipeline", summary="Save Workflow Template to Pipeline")
async def save_workflow_to_pipeline(
    data: SaveWorkflowToPipelineRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Save a workflow template as a pipeline in pipeline_mst table.

    This endpoint creates a pipeline record from a template and optionally creates
    PRs for workflow files using Aspora PR routing strategy.

    **PR Routing Strategy:**
    - Protected branches (main/master/pre-prod/qa/sandbox) → PR to pre-prod (fallback: stage-env)
    - Stage-env branches (stage-env/stage-env-copy) → PR to stage-env
    - Feature branches → PR to same branch

    **Request Body:**
    ```json
    {
        "template_code": "quick-release-template",
        "transaction_code": "svc-config-123",
        "pipeline_name": "My Workflow",
        "language": "go",
        "repo_url": "https://github.com/org/repo",
        "repo_branches": ["main", "stage-env", "feature-xyz"],
        "environment": "dev",
        "customized_steps": [...]
    }
    ```

    **Response:**
    Returns the created pipeline record with PR information for each branch.
    """
    try:
        import random
        import string
        import hashlib
        from app.db.models.pipeline_mst_model import PipelineMstModel
        from app.db.models.services_mst_model import ServicesMstModel
        from app.db.models.pipeline_vendor_mst_model import PipelineVendorMstModel
        from app.db.models.language_ref_model import LanguageRefModel
        from app.core.enum import EnvironmentEnum
        from app.services.workflow_generator import generate_workflow_yaml
        from app.strategies.aspora.pr_strategy import AsporaPRStrategy
        from app.integrations.github_integration import GitHubIntegration
        from app.core.config import settings
        from app.utils.github_app_token import get_token_for_org
        from sqlalchemy import select

        user, tenant = user_and_tenant

        # Normalize branches: use repo_branches if provided, otherwise use single repo_branch
        selected_branches = data.repo_branches if data.repo_branches else [data.repo_branch or "main"]

        # Validate environment first (needed for queries)
        try:
            environment_enum = EnvironmentEnum(data.environment.lower())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid environment: {data.environment}. Must be one of: dev, staging, prod"
            )

        # Check for existing workflows for the same source entity and branches
        # This prevents creating duplicate workflows for the same branch
        existing_pipelines_query = select(PipelineMstModel).where(
            PipelineMstModel.transaction_code == data.transaction_code,
            PipelineMstModel.table_name == "SERVICE_CONFIG",
        )
        existing_pipelines_result = await db.execute(existing_pipelines_query)
        existing_pipelines = existing_pipelines_result.scalars().all()

        # Check for branch conflicts
        conflicting_branches = []
        for pipeline in existing_pipelines:
            # Get branches from existing pipeline
            pipeline_branches = []
            if pipeline.deployment_config and isinstance(pipeline.deployment_config, dict):
                pipeline_branches = pipeline.deployment_config.get("selected_branches", [pipeline.repo_branch])

            # Check for overlap with selected branches
            for existing_branch in pipeline_branches:
                if existing_branch in selected_branches:
                    conflicting_branches.append({
                        "branch": existing_branch,
                        "existing_pipeline": pipeline.name,
                        "existing_pipeline_code": pipeline.code
                    })

        if conflicting_branches:
            # Format error message
            conflict_details = "\n".join([
                f"  - Branch '{conflict['branch']}' already used by workflow '{conflict['existing_pipeline']}'"
                for conflict in conflicting_branches
            ])
            raise HTTPException(
                status_code=409,
                detail=f"Cannot create workflow: one or more branches are already in use by existing workflows.\n{conflict_details}\n\nPlease either:\n1. Delete the existing workflow first, or\n2. Select different branches"
            )

        # Fetch template
        query = select(CicdTemplateRefModel).where(
            CicdTemplateRefModel.code == data.template_code,
            or_(
                CicdTemplateRefModel.tenant_mst_code == tenant.code,
                CicdTemplateRefModel.tenant_mst_code.is_(None)
            )
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"Template not found: {data.template_code}"
            )

        # Validate service config exists (transaction_code = service_config.code)
        from sqlalchemy.orm import selectinload
        from app.db.models.service_config_model import ServiceConfigModel
        service_config_query = select(ServiceConfigModel).where(
            ServiceConfigModel.code == data.transaction_code
        ).options(
            selectinload(ServiceConfigModel.infrastructure)
        )
        service_config_result = await db.execute(service_config_query)
        service_config = service_config_result.scalar_one_or_none()

        if not service_config:
            raise HTTPException(
                status_code=404,
                detail=f"Service config not found: {data.transaction_code}"
            )

        # Validate service exists and belongs to tenant
        service_query = select(ServicesMstModel).where(
            ServicesMstModel.code == service_config.services_mst_code,
            ServicesMstModel.tenants_mst_code == tenant.code
        ).options(
            selectinload(ServicesMstModel.infrastructure)
        )
        service_result = await db.execute(service_query)
        service = service_result.scalar_one_or_none()

        if not service:
            raise HTTPException(
                status_code=404,
                detail=f"Service not found for config: {data.transaction_code}"
            )

        # Get geo_loc_mst_code from service config or request
        geo_loc_mst_code = data.geo_loc_mst_code  # Use from request if provided

        if service_config and service_config.infrastructure:
            if not geo_loc_mst_code:
                geo_loc_mst_code = service_config.infrastructure.geo_loc_mst_code

        # Get GitHub Actions pipeline vendor (should exist for tenant)
        # Filter by tenant, agent type, and environment
        vendor_query = select(PipelineVendorMstModel).where(
            PipelineVendorMstModel.tenants_mst_code == tenant.code,
            PipelineVendorMstModel.pipeline_agent_enum == PipelineAgentEnum.github_actions,
            PipelineVendorMstModel.environment == environment_enum
        ).order_by(PipelineVendorMstModel.created_at.desc())

        vendor_result = await db.execute(vendor_query)
        pipeline_vendor = vendor_result.scalar_one_or_none()

        if not pipeline_vendor:
            raise HTTPException(
                status_code=404,
                detail="GitHub Actions pipeline vendor not configured for tenant"
            )

        # Get language reference
        # Try to match by name (case-insensitive) or find any language that contains the language name

        # Normalize language name (lowercase, remove spaces and special chars)
        normalized_lang = data.language.lower().replace(" ", "").replace("-", "").replace("maven", "").replace("gradle", "").replace("java", "java")

        # Build language matching conditions
        language_conditions = []

        # Try exact matches first
        language_conditions.append(LanguageRefModel.name.ilike(f"%{data.language}%"))

        # Try normalized partial matches
        if "java" in normalized_lang:
            # Match any Java variant
            language_conditions.append(LanguageRefModel.name.ilike("%java%"))
        elif "go" in normalized_lang:
            # Match Go
            language_conditions.append(LanguageRefModel.name.ilike("%go%"))
        elif "python" in normalized_lang or "py" in normalized_lang:
            # Match Python
            language_conditions.append(LanguageRefModel.name.ilike("%python%"))
            language_conditions.append(LanguageRefModel.name.ilike("%py%"))
        elif "node" in normalized_lang or "nodejs" in normalized_lang or "js" in normalized_lang:
            # Match Node.js
            language_conditions.append(LanguageRefModel.name.ilike("%node%"))
            language_conditions.append(LanguageRefModel.name.ilike("%javascript%"))

        # Build query
        language_query = select(LanguageRefModel).where(
            or_(*language_conditions) if language_conditions else LanguageRefModel.name.ilike("%%")
        ).order_by(
            # Prefer exact name matches first
            LanguageRefModel.name.asc()
        )

        language_result = await db.execute(language_query)
        language_ref_row = language_result.first()
        # Unpack the Row object to get the actual model instance
        language_ref = language_ref_row[0] if language_ref_row else None

        if not language_ref:
            raise HTTPException(
                status_code=404,
                detail=f"Language not found: {data.language}. Available languages must match language reference codes (e.g., 'java_21_lts_language_ref', 'go_1_23_language_ref')"
            )

        # Use user-provided pipeline name
        pipeline_name = data.pipeline_name.strip()

        # Generate pipeline code with hash for uniqueness
        unique_hash = hashlib.md5(f"{data.transaction_code}_{data.environment}_{','.join(selected_branches)}".encode()).hexdigest()[:8]
        pipeline_code = f"pipeline-{data.transaction_code}-{data.environment}-{unique_hash}"

        # Get steps - use customized steps if provided, otherwise use template steps
        template_steps = template.config.get("steps", []) if template.config else []
        steps_to_use = data.customized_steps if data.customized_steps else template_steps

        # Generate workflow YAML with all parameters
        # Extract AWS region and cluster name from infrastructure locator
        aws_region = "us-east-1"
        eks_cluster_name = "my-cluster"
        infrastructure_type = "eks"

        if service_config and service_config.infrastructure:
            infrastructure_type = service_config.infrastructure.infrastructuretype_ref_code.lower() if service_config.infrastructure.infrastructuretype_ref_code else "eks"
            # Extract from locator JSONB field
            if service_config.infrastructure.locator and isinstance(service_config.infrastructure.locator, dict):
                aws_region = service_config.infrastructure.locator.get("region", "us-east-1")
                eks_cluster_name = service_config.infrastructure.locator.get("cluster_name", "my-cluster")

        workflow_yaml = generate_workflow_yaml(
            language=data.language,
            steps=steps_to_use,
            service_name=service.name,
            environment=data.environment,
            branches=selected_branches,
            build_path=data.build_path if hasattr(data, 'build_path') else None,
            dockerfile_path=data.dockerfile_path if hasattr(data, 'dockerfile_path') else None,
            additional_trigger_paths=data.additional_trigger_paths if hasattr(data, 'additional_trigger_paths') else None,
            infrastructure_type=infrastructure_type,
            aws_region=aws_region,
            eks_cluster_name=eks_cluster_name
        )

        # Parse repository URL
        if "/" not in data.repo_url:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid repository format: {data.repo_url}. Expected 'owner/repo'"
            )
        owner, repo = data.repo_url.split("/", 1)

        # Get GitHub token via GitHub App installation
        github_token = await get_token_for_org(owner, db)

        # Initialize PR strategy
        pr_strategy = AsporaPRStrategy()

        # Fetch available branches from repository
        try:
            available_branches = GitHubIntegration.list_branches(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo
            )
            available_branch_names = [b.get("name", "") for b in available_branches]
        except Exception as e:
            print(f"Warning: Could not fetch branches from GitHub: {e}")
            # Use selected branches as available branches fallback
            available_branch_names = selected_branches

        # Create PRs for each selected branch
        pr_results = []
        for selected_branch in selected_branches:
            try:
                # Get PR routing information for this branch
                routing_info = pr_strategy.get_pr_routing_info(
                    selected_branch=selected_branch,
                    available_branches=available_branch_names,
                    create_secondary_pr=False  # Can be made configurable later
                )

                pr_target = routing_info["pr_target"]
                feature_branch_source = routing_info["feature_branch_source"]

                # Generate workflow file path
                workflow_file_path = f".github/workflows/{data.pipeline_name.lower().replace(' ', '-')}-{data.environment}.yml"

                # Check if workflow file already exists in target branch
                try:
                    existing_workflow = GitHubIntegration.get_file_content(
                        token=github_token,
                        base_url=settings.github_base_url,
                        owner=owner,
                        repo=repo,
                        file_path=workflow_file_path,
                        branch=pr_target
                    )
                    if existing_workflow and existing_workflow.get("exists"):
                        # Skip if content is the same
                        if existing_workflow.get("content", "") == workflow_yaml:
                            print(f"Workflow file already exists and is up to date for branch {pr_target}")
                            pr_results.append({
                                "branch": selected_branch,
                                "pr_target": pr_target,
                                "status": "skip",
                                "message": f"Workflow file already up to date in {pr_target}"
                            })
                            continue
                except Exception as e:
                    print(f"Could not check existing workflow file: {e}")

                # Create feature branch name
                feature_branch = f"ci/{data.pipeline_name.lower().replace(' ', '-')}-{unique_hash}"

                # Create feature branch
                try:
                    GitHubIntegration.create_branch(
                        token=github_token,
                        base_url=settings.github_base_url,
                        owner=owner,
                        repo=repo,
                        branch_name=feature_branch,
                        from_branch=feature_branch_source
                    )
                except Exception as e:
                    print(f"Warning: Could not create feature branch {feature_branch}: {e}")
                    # Try using the source branch directly
                    feature_branch = feature_branch_source

                # Create/update workflow file in feature branch
                GitHubIntegration.update_or_create_file(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    branch=feature_branch,
                    file_path=workflow_file_path,
                    content=workflow_yaml,
                    message=f"[CI/CD] Add workflow '{data.pipeline_name}' for {data.environment}"
                )

                # Check for existing PR or create new one
                existing_pr = GitHubIntegration.find_open_pr(
                    token=github_token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    head=feature_branch,
                    base=pr_target
                )

                if existing_pr:
                    pr_results.append({
                        "branch": selected_branch,
                        "pr_target": pr_target,
                        "status": "updated",
                        "pr_number": existing_pr.get("number"),
                        "pr_url": existing_pr.get("html_url"),
                        "head_sha": existing_pr.get("head_sha"),
                        "message": f"Updated existing PR #{existing_pr.get('number')} for {pr_target}"
                    })
                else:
                    # Create new PR
                    pr_title = f"[CI/CD] {data.pipeline_name} - {data.environment}"
                    pr_body = f"""## CI/CD Workflow Configuration

**Pipeline:** {data.pipeline_name}
**Environment:** {data.environment}
**Language:** {data.language}
**Template:** {template.name}

### Files Changed
- `{workflow_file_path}` - CI/CD workflow

### Steps ({len(steps_to_use)})
{chr(10).join(f"- {step.get('name', 'Unknown')}" for step in steps_to_use)}

---
*Generated by {settings.app_name}*"""

                    new_pr = GitHubIntegration.create_pull_request(
                        token=github_token,
                        base_url=settings.github_base_url,
                        owner=owner,
                        repo=repo,
                        head=feature_branch,
                        base=pr_target,
                        title=pr_title,
                        body=pr_body,
                        draft=False
                    )

                    pr_results.append({
                        "branch": selected_branch,
                        "pr_target": pr_target,
                        "status": "created",
                        "pr_number": new_pr.get("number"),
                        "pr_url": new_pr.get("html_url"),
                        "head_sha": new_pr.get("head_sha"),
                        "message": f"Created PR #{new_pr.get('number')} for {pr_target}"
                    })

            except Exception as e:
                import traceback
                print(f"Error creating PR for branch {selected_branch}: {str(e)}")
                print(traceback.format_exc())
                pr_results.append({
                    "branch": selected_branch,
                    "status": "error",
                    "error": str(e)
                })

        # Store workflow configuration in deployment_config
        deployment_config = {
            "template_code": data.template_code,
            "template_name": template.name,
            "workflow_yaml": workflow_yaml,
            "steps": steps_to_use,
            "language": data.language,
            "pr_results": pr_results,
            "selected_branches": selected_branches,
            "geo_loc_mst_code": geo_loc_mst_code
        }

        # Create pipeline record (use first branch as primary)
        pipeline = PipelineMstModel(
            code=pipeline_code,
            name=pipeline_name,
            transaction_code=data.transaction_code,
            table_name="SERVICE_CONFIG",
            tenant_code=tenant.code,
            pipeline_vendor_mst_code=pipeline_vendor.code,
            language_ref_code=language_ref.code,
            repo_url=data.repo_url,
            repo_branch=selected_branches[0],  # Store first branch as primary
            deployment_config=deployment_config,
            authentication_config=None,
        )

        db.add(pipeline)
        await db.commit()
        await db.refresh(pipeline)

        # Create gitops_workflow_detail records for PR tracking
        from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
        from app.core.enum import PRStatusEnum, WorkflowSourceTableEnum

        first_workflow_detail = None
        workflow_details_created = []

        for pr_result in pr_results:
            if pr_result.get("status") in ["created", "updated"]:
                # Parse PR URL to get PR number
                pr_number = pr_result.get("pr_number")
                pr_url = pr_result.get("pr_url")
                branch = pr_result.get("branch")
                head_sha = pr_result.get("head_sha")  # Get the commit SHA from PR

                # Create workflow detail record for each PR
                workflow_detail = GitopsWorkflowDetailModel(
                    code=f"{pipeline.code}-{branch}"[:100],  # Max 100 chars
                    name=f"{pipeline.name} - {branch}",
                    transaction_code=pipeline.code,
                    table_name=WorkflowSourceTableEnum.PIPELINE,
                    git_repository=data.repo_url,
                    git_branch=branch,
                    git_commit_sha=head_sha,  # Store the head commit SHA
                    pr_number=pr_number,
                    pr_url=pr_url,
                    pr_status=PRStatusEnum.PR_OPEN,
                    workflow_run_id=None,
                    workflow_run_url=None,
                    tenant_mst_code=tenant.code,
                    user_mst_code=user.code,
                    run_initiated_at=None,
                    run_completed_at=None
                )
                db.add(workflow_detail)
                workflow_details_created.append(workflow_detail)

                # Store the first workflow detail to link with pipeline
                if first_workflow_detail is None:
                    first_workflow_detail = workflow_detail

        # Commit all workflow detail records
        if workflow_details_created:
            await db.commit()

            # Refresh to get the IDs
            for wd in workflow_details_created:
                await db.refresh(wd)

            # Link the first workflow detail to the pipeline (for backward compatibility)
            if first_workflow_detail and first_workflow_detail.id:
                pipeline.gitops_workflow_id = first_workflow_detail.id
                await db.commit()
                await db.refresh(pipeline)

        # Count successful PRs
        successful_prs = [pr for pr in pr_results if pr.get("status") in ["created", "updated", "skip"]]

        return {
            "status": "success",
            "message": f"Workflow '{pipeline_name}' saved successfully with {len(successful_prs)}/{len(selected_branches)} PRs created",
            "pipeline_code": pipeline_code,
            "pipeline_name": pipeline_name,
            "template_used": template.name,
            "steps_count": len(steps_to_use),
            "branches_processed": len(selected_branches),
            "pr_results": pr_results,
            "workflow_yaml": workflow_yaml
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Error saving workflow to pipeline: {str(e)}")
        print(traceback.format_exc())
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save workflow to pipeline: {str(e)}")


@router.delete("/pipelines/{pipeline_code}", summary="Delete a Pipeline")
async def delete_pipeline(
    pipeline_code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete a saved pipeline (workflow) from pipeline_mst table.

    **Path Parameters:**
    - `pipeline_code`: The code of the pipeline to delete

    **Response:**
    ```json
    {
        "message": "Pipeline deleted successfully"
    }
    ```
    """
    try:
        from app.db.models.pipeline_mst_model import PipelineMstModel

        user, tenant = user_and_tenant

        # Fetch pipeline
        query = select(PipelineMstModel).where(
            PipelineMstModel.code == pipeline_code,
            PipelineMstModel.tenant_code == tenant.code
        )
        result = await db.execute(query)
        pipeline = result.scalar_one_or_none()

        if not pipeline:
            raise HTTPException(
                status_code=404,
                detail=f"Pipeline not found: {pipeline_code}"
            )

        # Delete the pipeline
        await db.delete(pipeline)
        await db.commit()

        return {"message": "Pipeline deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Error deleting pipeline: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to delete pipeline: {str(e)}")

