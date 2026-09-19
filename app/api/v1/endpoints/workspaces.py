"""
API endpoints for workspace management.
"""
import logging
from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user_and_tenant, get_db
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.domain.validators.workspace_rules import WorkspaceValidationError
from app.schemas.workspace_schemas import (
    AddUsersToWorkspaceRequest,
    AddUsersToWorkspaceResponse,
    CreateWorkspaceRequest,
    EligibleUsersResponse,
    GetAllWorkspacesRequest,
    RemoveWorkspaceMemberResponse,
    WorkspaceCreateResponse,
    WorkspaceMembersResponse,
    WorkspacesListResponse,
)
from app.services.workspace_service import WorkspaceService, WorkspaceServiceError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/create-workspace",
    response_model=WorkspaceCreateResponse,
    summary="Create Workspace",
)
async def create_workspace(
    data: CreateWorkspaceRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(
        get_current_user_and_tenant
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new workspace under the authenticated user's tenant.

    Only organization owners (`is_org_owner = True`) can create workspaces.
    The creator is automatically added to `workspace_user_mapping` with
    role=`owner` in the same transaction.

    Errors:
        400: Validation error
        403: Caller is not an organization owner
        409: A workspace with this name already exists in the organization
        500: Internal server error
    """
    user, tenant = user_and_tenant

    logger.info(
        "CREATE WORKSPACE - tenant isolation check",
        extra={
            "user_code": user.code,
            "user_email": user.email_id,
            "tenant_code": tenant.code,
            "tenant_subdomain": tenant.subdomain,
            "workspace_name": data.workspace_name,
            "endpoint": "/create-workspace",
        },
    )

    if not user.is_org_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only organization owners can create workspaces",
        )

    service = WorkspaceService(db)

    try:
        return await service.create_workspace(user=user, tenant=tenant, data=data)
    except WorkspaceValidationError as e:
        logger.warning(f"Validation error in create_workspace: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except WorkspaceServiceError as e:
        logger.warning(f"Service error in create_workspace: {e.detail}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in create_workspace: {str(e)}",
            exc_info=True,
            extra={"tenant_code": tenant.code, "workspace_name": data.workspace_name},
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{workspace_code}/add-users",
    response_model=AddUsersToWorkspaceResponse,
    summary="Add Users to Workspace",
)
async def add_users_to_workspace(
    data: AddUsersToWorkspaceRequest,
    workspace_code: str = Path(..., min_length=1, max_length=100),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(
        get_current_user_and_tenant
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Bulk-add users to an existing workspace with a per-user role.

    Permission: caller must be an organization owner OR have role
    `owner`/`admin` in the target workspace. The role `owner` cannot be
    assigned via this endpoint — only the workspace creator is owner.

    Best-effort semantics: per-user failures (user not found, cross-tenant,
    already mapped) do not abort the request. The response reports each
    bucket separately.

    Errors:
        400: Validation error (empty list, duplicate user_code, owner role)
        403: Caller lacks permission
        404: Workspace not found in caller's tenant
        409: Workspace is archived
    """
    caller, tenant = user_and_tenant

    logger.info(
        "ADD USERS TO WORKSPACE - tenant isolation check",
        extra={
            "user_code": caller.code,
            "tenant_code": tenant.code,
            "workspace_code": workspace_code,
            "user_count": len(data.users),
            "endpoint": "/add-users",
        },
    )

    service = WorkspaceService(db)

    try:
        return await service.add_users(
            caller=caller,
            tenant=tenant,
            workspace_code=workspace_code,
            data=data,
        )
    except WorkspaceValidationError as e:
        logger.warning(f"Validation error in add_users_to_workspace: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except WorkspaceServiceError as e:
        logger.warning(f"Service error in add_users_to_workspace: {e.detail}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in add_users_to_workspace: {str(e)}",
            exc_info=True,
            extra={"tenant_code": tenant.code, "workspace_code": workspace_code},
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{workspace_code}/members",
    response_model=WorkspaceMembersResponse,
    summary="List Workspace Members",
)
async def list_workspace_members(
    workspace_code: str = Path(..., min_length=1, max_length=100),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(
        get_current_user_and_tenant
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    List active members of a workspace, joined with user identity
    (first_name, last_name, email_id, role).

    Tenant-isolated; returns 404 if the workspace does not exist in the
    caller's tenant.
    """
    user, tenant = user_and_tenant

    logger.info(
        "LIST WORKSPACE MEMBERS",
        extra={
            "user_code": user.code,
            "tenant_code": tenant.code,
            "workspace_code": workspace_code,
            "endpoint": "/{workspace_code}/members",
        },
    )

    service = WorkspaceService(db)

    try:
        return await service.list_members(tenant=tenant, workspace_code=workspace_code)
    except WorkspaceServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in list_workspace_members: {str(e)}",
            exc_info=True,
            extra={"tenant_code": tenant.code, "workspace_code": workspace_code},
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{workspace_code}/members/{user_code}",
    response_model=RemoveWorkspaceMemberResponse,
    summary="Remove Member from Workspace",
)
async def remove_workspace_member(
    workspace_code: str = Path(..., min_length=1, max_length=100),
    user_code: str = Path(..., min_length=1, max_length=100),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(
        get_current_user_and_tenant
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Soft-remove a single user from a workspace.

    Permission: caller must be an organization owner OR have role
    `owner`/`admin` in the target workspace. The workspace `owner` cannot
    be removed via this endpoint.

    Errors:
        403: Caller lacks permission
        404: Workspace not found in caller's tenant, or user is not a member
        409: Workspace is archived, or target is the workspace owner
    """
    caller, tenant = user_and_tenant

    logger.info(
        "REMOVE WORKSPACE MEMBER",
        extra={
            "user_code": caller.code,
            "tenant_code": tenant.code,
            "workspace_code": workspace_code,
            "target_user_code": user_code,
            "endpoint": "/{workspace_code}/members/{user_code}",
        },
    )

    service = WorkspaceService(db)

    try:
        return await service.remove_member(
            caller=caller,
            tenant=tenant,
            workspace_code=workspace_code,
            target_user_code=user_code,
        )
    except WorkspaceServiceError as e:
        logger.warning(f"Service error in remove_workspace_member: {e.detail}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in remove_workspace_member: {str(e)}",
            exc_info=True,
            extra={
                "tenant_code": tenant.code,
                "workspace_code": workspace_code,
                "target_user_code": user_code,
            },
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{workspace_code}/eligible-users",
    response_model=EligibleUsersResponse,
    summary="List Users Eligible to Add to Workspace",
)
async def list_eligible_users(
    workspace_code: str = Path(..., min_length=1, max_length=100),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(
        get_current_user_and_tenant
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Return tenant users who are NOT yet mapped to this workspace. Powers
    the Add-Users picker without requiring client-side filtering.
    """
    user, tenant = user_and_tenant

    logger.info(
        "LIST ELIGIBLE USERS",
        extra={
            "user_code": user.code,
            "tenant_code": tenant.code,
            "workspace_code": workspace_code,
            "endpoint": "/{workspace_code}/eligible-users",
        },
    )

    service = WorkspaceService(db)

    try:
        return await service.list_eligible_users(
            tenant=tenant, workspace_code=workspace_code
        )
    except WorkspaceServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in list_eligible_users: {str(e)}",
            exc_info=True,
            extra={"tenant_code": tenant.code, "workspace_code": workspace_code},
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/get-all-workspaces",
    response_model=WorkspacesListResponse,
    summary="Get Workspaces for Current User",
)
async def get_all_workspaces(
    data: GetAllWorkspacesRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(
        get_current_user_and_tenant
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all workspaces the authenticated user has access to within their
    tenant. Each workspace includes the user's role (owner, admin, edit,
    read_only) sourced from `workspace_user_mapping`.
    """
    user, tenant = user_and_tenant

    logger.info(
        "GET ALL WORKSPACES - user-scoped listing",
        extra={
            "user_code": user.code,
            "tenant_code": tenant.code,
            "is_active_filter": data.is_active,
            "endpoint": "/get-all-workspaces",
        },
    )

    service = WorkspaceService(db)

    try:
        result = await service.get_user_workspaces(
            user_code=user.code,
            tenant_code=tenant.code,
            is_active=data.is_active,
            skip=data.skip,
            limit=data.limit,
        )
        return {
            "total": result["total"],
            "skip": data.skip,
            "limit": data.limit,
            "workspaces": result["workspaces"],
        }
    except WorkspaceServiceError as e:
        logger.warning(f"Service error in get_all_workspaces: {e.detail}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in get_all_workspaces: {str(e)}",
            exc_info=True,
            extra={"tenant_code": tenant.code, "user_code": user.code},
        )
        raise HTTPException(status_code=500, detail=str(e))
