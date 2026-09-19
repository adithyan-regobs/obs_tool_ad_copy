"""
GitOps Workflows API Endpoints

API endpoints for listing and managing PR workflows.
"""
from typing import Optional, List
from datetime import date
from math import ceil
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
from app.schemas.gitops_workflow_schemas import (
    GitopsWorkflowListItem,
    GitopsWorkflowListResponse,
    PRHistoryResponse,
    ServiceOption,
    CaseTypeOption
)
from app.core.enum import PRStatusEnum, WorkflowSourceTableEnum

router = APIRouter()


@router.get("", response_model=GitopsWorkflowListResponse)
async def list_workflows(
    status: Optional[PRStatusEnum] = Query(None, description="Filter by PR status (PR_OPEN/PR_MERGED/PR_CLOSED)"),
    user_code: Optional[str] = Query(None, description="Filter by user code"),
    date_from: Optional[date] = Query(None, description="Filter from date (YYYY-MM-DD)"),
    date_to: Optional[date] = Query(None, description="Filter to date (YYYY-MM-DD)"),
    service_code: Optional[str] = Query(None, description="Filter by service code"),
    case_type: Optional[str] = Query(None, description="Filter by case type (for infrastructure workflows)"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    List GitOps workflows with filters and pagination.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: Only returns workflows for authenticated user's tenant

    Query Parameters:
        - status: Optional PR status filter (PR_OPEN, PR_MERGED, PR_CLOSED)
        - user_code: Optional user code filter
        - date_from: Optional start date filter (YYYY-MM-DD)
        - date_to: Optional end date filter (YYYY-MM-DD)
        - service_code: Optional service code filter
        - case_type: Optional case type filter (for infrastructure workflows)
        - page: Page number (default: 1)
        - limit: Items per page (default: 20, max: 100)

    Response:
        - items: List of workflow details with user email, name, service information, and case type
        - total: Total count of workflows matching filters
        - page: Current page number
        - limit: Items per page
        - pages: Total pages

    Example:
        GET /api/v1/gitops-workflows?status=PR_OPEN&service_code=SVC001&case_type=s3-bucket&date_from=2025-01-01&date_to=2025-01-31&page=1&limit=20
    """
    user, tenant = current_user_tenant
    repository = GitopsWorkflowDetailRepository(db)

    workflows, total, service_info, case_type_info = await repository.list_workflows_with_filters(
        tenant_code=tenant.code,
        pr_status=status,
        user_code=user_code,
        date_from=date_from,
        date_to=date_to,
        service_code=service_code,
        case_type=case_type,
        page=page,
        limit=limit
    )

    # Transform to response with user details, service information, and case type
    items = []
    for workflow in workflows:
        workflow_service_info = service_info.get(workflow.id, {})
        workflow_case_info = case_type_info.get(workflow.id, {})
        item = GitopsWorkflowListItem(
            id=workflow.id,
            code=workflow.code,
            name=workflow.name,
            git_repository=workflow.git_repository,
            git_branch=workflow.git_branch,
            git_commit_sha=workflow.git_commit_sha,
            pr_number=workflow.pr_number,
            pr_url=workflow.pr_url,
            pr_status=workflow.pr_status,
            workflow_run_id=workflow.workflow_run_id,
            workflow_run_url=workflow.workflow_run_url,
            run_initiated_at=workflow.run_initiated_at,
            run_completed_at=workflow.run_completed_at,
            created_at=workflow.created_at,
            tenant_mst_code=workflow.tenant_mst_code,
            user_mst_code=workflow.user_mst_code,
            user_email=workflow.user.email_id if workflow.user else None,
            user_name=workflow.user.name if workflow.user else None,
            service_name=workflow_service_info.get("service_name"),
            service_code=workflow_service_info.get("service_code"),
            case_type=workflow_case_info.get("case_type")
        )
        items.append(item)

    pages = ceil(total / limit) if total > 0 else 1

    return GitopsWorkflowListResponse(
        items=items,
        total=total,
        page=page,
        limit=limit,
        pages=pages
    )


@router.get("/users", response_model=List[dict])
async def get_workflow_users(
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get list of users who have created workflows.

    Used for user filter dropdown in frontend.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Response:
        List of users with user_code, user_email, and user_name

    Example:
        GET /api/v1/gitops-workflows/users
    """
    user, tenant = current_user_tenant
    repository = GitopsWorkflowDetailRepository(db)

    users = await repository.get_users_with_workflows(tenant_code=tenant.code)

    return users


@router.get("/services", response_model=List[ServiceOption])
async def get_workflow_services(
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get list of services that have workflows for the service filter dropdown.

    Used for service filter dropdown in frontend.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Response:
        List of services with service_code and service_name

    Example:
        GET /api/v1/gitops-workflows/services
    """
    user, tenant = current_user_tenant
    repository = GitopsWorkflowDetailRepository(db)

    services = await repository.get_services_with_workflows(tenant_code=tenant.code)

    return services


@router.get("/case-types", response_model=List[CaseTypeOption])
async def get_workflow_case_types(
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get list of case types for infrastructure workflows for the case type filter dropdown.

    Used for case type filter dropdown in frontend.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Response:
        List of case types (e.g., 's3-bucket', 'sqs-queue', 'dynamodb-table')

    Example:
        GET /api/v1/gitops-workflows/case-types
    """
    user, tenant = current_user_tenant
    repository = GitopsWorkflowDetailRepository(db)

    case_types = await repository.get_case_types_with_workflows(tenant_code=tenant.code)

    return case_types


@router.get("/history", response_model=PRHistoryResponse)
async def get_pr_history(
    transaction_code: str = Query(..., description="Transaction code (service_config.code for Terragrunt, dockerfile_workflow_code for Dockerfile)"),
    table_name: WorkflowSourceTableEnum = Query(..., description="Table name: SERVICE_CONFIG or SERVICE_CONFIG_DOCKERFILE"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page (max 100)"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get PR history by polymorphic reference (transaction_code + table_name).

    Used by PR History Modal to show all PRs for a specific entity:
    - Terragrunt PRs: transaction_code = service_config.code, table_name = SERVICE_CONFIG
    - Dockerfile PRs: transaction_code = dockerfile_workflow_code (SCDF_xxx), table_name = SERVICE_CONFIG_DOCKERFILE

    Security:
        - JWT authentication required
        - Tenant isolation enforced: Only returns PRs for authenticated user's tenant

    Query Parameters:
        - transaction_code: Required - the code to lookup
        - table_name: Required - SERVICE_CONFIG or SERVICE_CONFIG_DOCKERFILE
        - page: Page number (default: 1)
        - limit: Items per page (default: 20, max: 100)

    Response:
        - items: List of PR history items with user details
        - total: Total count of PRs
        - page/limit/pages: Pagination info
        - transaction_code/table_name: Echo back the lookup parameters

    Example:
        GET /api/v1/gitops-workflows/history?transaction_code=service-config-abc&table_name=SERVICE_CONFIG
    """
    user, tenant = current_user_tenant
    repository = GitopsWorkflowDetailRepository(db)

    workflows, total = await repository.get_by_transaction_code_and_table(
        transaction_code=transaction_code,
        table_name=table_name,
        tenant_code=tenant.code,
        page=page,
        limit=limit
    )

    # Transform to response with user details
    items = []
    for workflow in workflows:
        item = GitopsWorkflowListItem(
            id=workflow.id,
            code=workflow.code,
            name=workflow.name,
            git_repository=workflow.git_repository,
            git_branch=workflow.git_branch,
            git_commit_sha=workflow.git_commit_sha,
            pr_number=workflow.pr_number,
            pr_url=workflow.pr_url,
            pr_status=workflow.pr_status,
            workflow_run_id=workflow.workflow_run_id,
            workflow_run_url=workflow.workflow_run_url,
            run_initiated_at=workflow.run_initiated_at,
            run_completed_at=workflow.run_completed_at,
            created_at=workflow.created_at,
            tenant_mst_code=workflow.tenant_mst_code,
            user_mst_code=workflow.user_mst_code,
            user_email=workflow.user.email_id if workflow.user else None,
            user_name=workflow.user.name if workflow.user else None,
            transaction_code=workflow.transaction_code,
            table_name=workflow.table_name
        )
        items.append(item)

    pages = ceil(total / limit) if total > 0 else 1

    return PRHistoryResponse(
        items=items,
        total=total,
        page=page,
        limit=limit,
        pages=pages,
        transaction_code=transaction_code,
        table_name=table_name
    )


@router.get("/dockerfile-history", response_model=PRHistoryResponse)
async def get_dockerfile_pr_history(
    service_config_id: int = Query(..., description="Service config ID to fetch Dockerfile PRs for"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page (max 100)"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get Dockerfile PR history for a service config.

    Fetches all Dockerfile workflow PRs across all branches/repositories for a given service config.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: Only returns PRs for authenticated user's tenant

    Query Parameters:
        - service_config_id: Required - Service config ID
        - page: Page number (default: 1)
        - limit: Items per page (default: 20, max 100)

    Response:
        - items: List of Dockerfile PR history items with user details
        - total: Total count of PRs
        - page/limit/pages: Pagination info

    Example:
        GET /api/v1/gitops-workflows/dockerfile-history?service_config_id=123
    """
    from app.services.gitops_workflow_service import GitopsWorkflowService

    user, tenant = current_user_tenant
    service = GitopsWorkflowService(db)

    # Get workflows via service layer
    workflows, total = await service.get_dockerfile_pr_history(
        service_config_id=service_config_id,
        tenant_code=tenant.code,
        page=page,
        limit=limit
    )

    # Transform to response with user details
    items = []
    for workflow in workflows:
        item = GitopsWorkflowListItem(
            id=workflow.id,
            code=workflow.code,
            name=workflow.name,
            git_repository=workflow.git_repository,
            git_branch=workflow.git_branch,
            git_commit_sha=workflow.git_commit_sha,
            pr_number=workflow.pr_number,
            pr_url=workflow.pr_url,
            pr_status=workflow.pr_status,
            workflow_run_id=workflow.workflow_run_id,
            workflow_run_url=workflow.workflow_run_url,
            run_initiated_at=workflow.run_initiated_at,
            run_completed_at=workflow.run_completed_at,
            created_at=workflow.created_at,
            tenant_mst_code=workflow.tenant_mst_code,
            user_mst_code=workflow.user_mst_code,
            user_email=workflow.user.email_id if workflow.user else None,
            user_name=workflow.user.name if workflow.user else None,
            transaction_code=workflow.transaction_code,
            table_name=workflow.table_name
        )
        items.append(item)

    pages = ceil(total / limit) if total > 0 else 1

    return PRHistoryResponse(
        items=items,
        total=total,
        page=page,
        limit=limit,
        pages=pages,
        transaction_code="",  # Empty for multi-code queries
        table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE
    )


@router.get("/pipeline-history", response_model=PRHistoryResponse)
async def get_pipeline_pr_history(
    transaction_code: str = Query(..., description="Transaction code (service_config.code)"),
    table_name: str = Query("SERVICE_CONFIG", description="Source table discriminator (SERVICE_CONFIG, INFRASTRUCTURE)"),
    geo_loc_mst_code: Optional[str] = Query(None, description="Geo location code"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page (max 100)"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get Pipeline PR history by transaction code.

    Fetches all Pipeline workflow PRs matching the specified criteria.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: Only returns PRs for authenticated user's tenant

    Query Parameters:
        - transaction_code: Required - Transaction code (service_config.code)
        - geo_loc_mst_code: Optional - Geo location code
        - page: Page number (default: 1)
        - limit: Items per page (default: 20, max 100)

    Response:
        - items: List of Pipeline PR history items with user details
        - total: Total count of PRs
        - page/limit/pages: Pagination info

    Example:
        GET /api/v1/gitops-workflows/pipeline-history?transaction_code=svc-config-123&geo_loc_mst_code=london
    """
    from app.services.gitops_workflow_service import GitopsWorkflowService

    user, tenant = current_user_tenant
    service = GitopsWorkflowService(db)

    # Get workflows via service layer
    workflows, total = await service.get_pipeline_pr_history(
        transaction_code=transaction_code,
        table_name=table_name,
        tenant_code=tenant.code,
        geo_loc_mst_code=geo_loc_mst_code,
        page=page,
        limit=limit
    )

    # Transform to response with user details
    items = []
    for workflow in workflows:
        item = GitopsWorkflowListItem(
            id=workflow.id,
            code=workflow.code,
            name=workflow.name,
            git_repository=workflow.git_repository,
            git_branch=workflow.git_branch,
            git_commit_sha=workflow.git_commit_sha,
            pr_number=workflow.pr_number,
            pr_url=workflow.pr_url,
            pr_status=workflow.pr_status,
            workflow_run_id=workflow.workflow_run_id,
            workflow_run_url=workflow.workflow_run_url,
            run_initiated_at=workflow.run_initiated_at,
            run_completed_at=workflow.run_completed_at,
            created_at=workflow.created_at,
            tenant_mst_code=workflow.tenant_mst_code,
            user_mst_code=workflow.user_mst_code,
            user_email=workflow.user.email_id if workflow.user else None,
            user_name=workflow.user.name if workflow.user else None,
            transaction_code=workflow.transaction_code,
            table_name=workflow.table_name
        )
        items.append(item)

    pages = ceil(total / limit) if total > 0 else 1

    return PRHistoryResponse(
        items=items,
        total=total,
        page=page,
        limit=limit,
        pages=pages,
        transaction_code="",  # Empty for multi-code queries
        table_name=WorkflowSourceTableEnum.PIPELINE
    )
