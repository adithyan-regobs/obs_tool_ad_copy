"""
Service Permissions Endpoints

API endpoints for permission cache management and permission checking.
"""

from typing import Tuple
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.permission_cache_service import PermissionCacheService
from app.services.permission_service import PermissionService
from app.services.permission_management_service import PermissionManagementService
from app.utils.permission_helper import PermissionHelper
from app.schemas.permission_schemas import (
    RebuildCacheRequest,
    RebuildCacheResponse,
    CheckPermissionRequest,
    CheckPermissionResponse,
    ServicePermissionCreate,
    ServicePermissionResponse,
    PermissionListResponse,
    ManageableScopesResponse,
    AssigneesResponse,
    PoliciesResponse
)
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.core.enum import EnvironmentEnum

router = APIRouter()


@router.post("/rebuild-cache", response_model=RebuildCacheResponse, summary="Rebuild Permission Cache")
async def rebuild_cache(
    data: RebuildCacheRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Rebuild permission cache for a user.

    This endpoint:
    1. Gets user's direct permissions from service_user_permission table
    2. Gets role-based permissions
    3. Expands RG/App level permissions to services
    4. Saves expanded permissions to cache table

    Request Body:
        - user_mst_code: User to rebuild cache for
        - role_type_codes: List of user's role type codes (from role_type_ref)
        - environment: Optional environment filter (dev, staging, prod)

    Returns:
        - user_mst_code: User code
        - environment: Environment (if specified)
        - permissions_count: Number of permissions in cache
        - cache_id: Cache row ID
    """
    try:
        # Get tenant from JWT
        user, tenant = user_and_tenant

        # Convert environment string to enum if provided
        environment = None
        if data.environment:
            try:
                environment = EnvironmentEnum(data.environment)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid environment: {data.environment}"
                )

        # Call cache service
        cache_service = PermissionCacheService(db)
        result = await cache_service.rebuild_cache(
            user_mst_code=data.user_mst_code,
            tenants_mst_code=tenant.code,
            role_type_codes=data.role_type_codes,
            environment=environment
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/check", response_model=CheckPermissionResponse, summary="Check Permission")
async def check_permission(
    data: CheckPermissionRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Check if a user has specific permission on a service.

    This endpoint reads from cache table (fast lookup).
    Cache must be built first using /rebuild-cache endpoint.

    Request Body:
        - user_mst_code: User to check
        - service_mst_code: Service to check permission for
        - policy_ref_code: Policy (service_admin, service_manager, service_support)
        - environment: Optional environment filter

    Returns:
        - has_permission: True if user has permission, False otherwise
    """
    try:
        # Get tenant from JWT
        user, tenant = user_and_tenant

        # Convert environment string to enum if provided
        environment = None
        if data.environment:
            try:
                environment = EnvironmentEnum(data.environment)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid environment: {data.environment}"
                )

        # Call permission service
        permission_service = PermissionService(db)
        has_perm = await permission_service.has_permission(
            user_mst_code=data.user_mst_code,
            tenants_mst_code=tenant.code,
            service_mst_code=data.service_mst_code,
            policy_ref_code=data.policy_ref_code,
            environment=environment
        )

        return {"has_permission": has_perm}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ Permission Management UI Endpoints ============

async def _check_manage_access(
    db: AsyncSession,
    user: UserMstModel,
    tenant: TenantsMstModel,
    scope_type: str = None,
    scope_code: str = None
) -> None:
    """Check if user has permission to manage permissions."""
    perm_helper = PermissionHelper(db)

    if await perm_helper.is_org_owner(user):
        return

    if scope_type == "service" and scope_code:
        has_manage = await perm_helper.check_manage_access(user, tenant, scope_code)
        if not has_manage:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to manage this service"
            )
    elif scope_type == "application":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only organization owners can manage application permissions"
        )


@router.get("/manageable-scopes", response_model=ManageableScopesResponse)
async def get_manageable_scopes(
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Get services and applications the current user can manage permissions for."""
    user, tenant = user_and_tenant
    service = PermissionManagementService(db)
    scopes = await service.get_manageable_scopes(user, tenant)
    return ManageableScopesResponse(scopes=scopes, total=len(scopes))


@router.get("/manageable-applications")
async def get_manageable_applications(
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Get applications the user can assign permissions for."""
    user, tenant = user_and_tenant
    service = PermissionManagementService(db)
    applications = await service.get_manageable_applications(user, tenant)
    return {"applications": applications, "total": len(applications)}


@router.get("/manageable-services/{application_code}")
async def get_manageable_services_for_app(
    application_code: str,
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Get services the user can assign permissions for within an application."""
    user, tenant = user_and_tenant
    service = PermissionManagementService(db)
    services = await service.get_manageable_services_for_application(user, tenant, application_code)
    return {"services": services, "total": len(services)}


@router.get("/permissions/all", response_model=PermissionListResponse)
async def get_all_permissions(
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Get all permissions the current user can manage."""
    user, tenant = user_and_tenant
    service = PermissionManagementService(db)
    permissions = await service.get_all_permissions(user, tenant)
    return PermissionListResponse(permissions=permissions, total=len(permissions))


@router.get("/permissions/{scope_type}/{scope_code}", response_model=PermissionListResponse)
async def get_permissions_for_scope(
    scope_type: str,
    scope_code: str,
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Get all permissions for a specific service or application."""
    if scope_type not in ("service", "application"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_type must be 'service' or 'application'"
        )

    user, tenant = user_and_tenant
    await _check_manage_access(db, user, tenant, scope_type, scope_code)

    service = PermissionManagementService(db)
    permissions = await service.get_permissions_for_scope(tenant, scope_type, scope_code)
    return PermissionListResponse(permissions=permissions, total=len(permissions))


@router.post("/permissions", response_model=ServicePermissionResponse, status_code=status.HTTP_201_CREATED)
async def create_permission(
    data: ServicePermissionCreate,
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Create a new service permission."""
    user, tenant = user_and_tenant
    scope_type = data.scope_type
    scope_code = data.services_mst_code or data.applications_mst_code

    await _check_manage_access(db, user, tenant, scope_type, scope_code)

    service = PermissionManagementService(db)
    permission = await service.create_permission(tenant, data)
    await db.commit()

    # Rebuild cache for affected user(s)
    try:
        cache_service = PermissionCacheService(db)

        if data.assignment_type == "user" and data.user_mst_code:
            await cache_service.rebuild_cache(
                user_mst_code=data.user_mst_code,
                tenants_mst_code=tenant.code
            )
        elif data.assignment_type == "role" and data.role_type_ref_code:
            await cache_service.rebuild_cache_for_role(
                role_type_ref_code=data.role_type_ref_code,
                tenants_mst_code=tenant.code
            )
    except Exception:
        # Don't fail if cache rebuild fails
        pass

    return permission


@router.delete("/permissions/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_permission(
    code: str,
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Delete a permission by code."""
    user, tenant = user_and_tenant
    perm_helper = PermissionHelper(db)

    if not await perm_helper.is_org_owner(user):
        from app.repository.service_user_permission_repository import ServiceUserPermissionRepository
        repo = ServiceUserPermissionRepository(db)
        perm = await repo.get_by_code(code, tenant.code)

        if not perm:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Permission '{code}' not found")

        if perm.services_mst_code:
            await _check_manage_access(db, user, tenant, "service", perm.services_mst_code)
        elif perm.applications_mst_code:
            await _check_manage_access(db, user, tenant, "application", perm.applications_mst_code)

    service = PermissionManagementService(db)
    deleted = await service.delete_permission(tenant, code)

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Permission '{code}' not found")

    await db.commit()


@router.get("/assignees", response_model=AssigneesResponse)
async def get_assignees(
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Get available users and roles for permission assignment."""
    user, tenant = user_and_tenant
    perm_helper = PermissionHelper(db)

    if not await perm_helper.is_org_owner(user):
        # Check ALL cache rows (across all environments) for can_manage
        all_caches = await perm_helper.cache_service.cache_repo.get_all_caches_for_user(
            user_mst_code=user.code, tenants_mst_code=tenant.code
        )
        has_any_manage = False
        for cache in all_caches:
            if cache.permissions:
                if any(p.get("can_manage", False) for p in cache.permissions):
                    has_any_manage = True
                    break
        if not has_any_manage:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to manage any services"
            )

    service = PermissionManagementService(db)
    result = await service.get_assignees(tenant, current_user=user)
    return AssigneesResponse(users=result["users"], roles=result["roles"])


@router.get("/policies", response_model=PoliciesResponse)
async def get_policies(
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """Get available policies for permission assignment."""
    user, tenant = user_and_tenant
    perm_helper = PermissionHelper(db)

    if not await perm_helper.is_org_owner(user):
        # Check ALL cache rows (across all environments) for can_manage
        all_caches = await perm_helper.cache_service.cache_repo.get_all_caches_for_user(
            user_mst_code=user.code, tenants_mst_code=tenant.code
        )
        has_any_manage = False
        for cache in all_caches:
            if cache.permissions:
                if any(p.get("can_manage", False) for p in cache.permissions):
                    has_any_manage = True
                    break
        if not has_any_manage:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to manage any services"
            )

    service = PermissionManagementService(db)
    policies = await service.get_policies()
    return PoliciesResponse(policies=policies)


@router.get("/existing-permissions")
async def get_existing_permissions_for_assignee(
    assignee_type: str,
    assignee_code: str,
    application_code: str,
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get existing permissions for a specific user or role within an application.

    Used to pre-populate environment checkboxes in the assign permissions UI.

    Query Parameters:
        - assignee_type: "user" or "role"
        - assignee_code: User code or Role code
        - application_code: Application code to filter services

    Returns:
        Dict mapping service_code -> list of environments with existing permissions
    """
    if assignee_type not in ("user", "role"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="assignee_type must be 'user' or 'role'"
        )

    _, tenant = user_and_tenant
    service = PermissionManagementService(db)
    return await service.get_existing_permissions_for_assignee(
        tenant=tenant,
        assignee_type=assignee_type,
        assignee_code=assignee_code,
        application_code=application_code
    )
