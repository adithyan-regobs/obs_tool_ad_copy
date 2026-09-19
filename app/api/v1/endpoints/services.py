from typing import Tuple
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.services_mst_service import ServicesMstService
from app.services.permission_cache_service import PermissionCacheService
from app.services.permission_service import PermissionService
from app.utils.permission_helper import PermissionHelper
from app.domain.validators.services_mst_rules import ServiceValidationError
from app.schemas.service_schemas import (
    GetAllServicesRequest,
    ServicesListResponse,
    GetServiceByCodeRequest,
    ServiceDetailResponse,
    CreateServiceRequest,
    CreateServiceResponse,
    DeleteServiceRequest,
    DeleteServiceResponse,
    UpdateServiceOwnerRequest,
    UpdateServiceOwnerResponse,
)
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

router = APIRouter()


@router.post("/get-all-services", response_model=ServicesListResponse, summary="Get All Services")
async def get_all_services(
    data: GetAllServicesRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all services with complete information including tenant, application,
    resource group, alert counts, and dependencies.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: Only returns services for authenticated user's tenant
        - tenant_code automatically injected from JWT token

    Request Body:
        - application_code (optional): Filter by application
        - is_active (optional): Filter by status (true/false)
        - skip (optional): Pagination offset (default: 0)
        - limit (optional): Page size (default: 100, max: 500)

    Response:
        - total: Total number of matching services
        - skip: Pagination offset used
        - limit: Page size used
        - services: List of service details (includes description field)

    Examples:
        # Get all services
        POST /api/v1/services/get-all-services
        Body: {}

        # Get services for specific application
        POST /api/v1/services/get-all-services
        Body: {"application_code": "ecommerce_app"}

        # With pagination
        POST /api/v1/services/get-all-services
        Body: {"application_code": "ecommerce_app", "skip": 0, "limit": 20}
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Check if user is org_owner (bypasses permission checks)
        perm_helper = PermissionHelper(db)
        if await perm_helper.is_org_owner(user):
            # Org owner can see all services - no filter
            allowed_service_codes = None
        else:
            # Get allowed service codes from permission cache
            cache_service = PermissionCacheService(db)
            allowed_service_codes = await cache_service.get_allowed_services(
                user_mst_code=user.code,
                tenants_mst_code=tenant.code
            )

        # Initialize service layer
        service = ServicesMstService(db)

        # Call service layer with tenant_code from JWT (tenant isolation enforced)
        result = await service.get_all_services(
            tenant_code=tenant.code,
            user_code=user.code,
            application_code=data.application_code,
            resource_group_mst_code=data.resource_group_mst_code,
            is_active=data.is_active,
            search_query=data.search_query,
            skip=data.skip,
            limit=data.limit,
            allowed_service_codes=allowed_service_codes  # None for org_owner (no filter)
        )

        return {
            "total": result["total"],
            "skip": data.skip,
            "limit": data.limit,
            "services": result["services"]
        }
    except HTTPException:
        raise
    except ServiceValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/get-service-detail", response_model=ServiceDetailResponse, summary="Get Service Detail")
async def get_service_detail(
    data: GetServiceByCodeRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get complete service details by service code with tenant isolation.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: service must belong to authenticated user's tenant
        - tenant_code automatically injected from JWT token

    Request Body:
        - service_code: Auto-generated service code

    Response:
        Complete service details including:
        - Basic info (id, code, name, description, status)
        - Relationships (tenant, application, resource group)
        - Alert counts (configured, total, failed)
        - Infrastructure and dependency counts
        - Timestamps

    Returns:
        ServiceDetailResponse with complete service information

    Raises:
        404: Service not found or doesn't belong to tenant
        422: Validation error (missing or invalid fields)
        500: Internal server error

    Example:
        POST /api/v1/services/get-service-detail
        Body: {
            "service_code": "payment_api"
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Check if user is org_owner (bypasses permission checks)
        perm_helper = PermissionHelper(db)
        if not await perm_helper.is_org_owner(user):
            # Not org_owner - check permission for specific service (optimized)
            perm_service = PermissionService(db)
            has_permission = await perm_service.has_any_service_permission(
                user_mst_code=user.code,
                tenants_mst_code=tenant.code,
                service_mst_code=data.service_code
            )

            if not has_permission:
                raise HTTPException(
                    status_code=403,
                    detail=f"You don't have permission to access service '{data.service_code}'"
                )

        # Initialize service layer
        service = ServicesMstService(db)

        # Call service layer with tenant_code from JWT (tenant isolation enforced)
        result = await service.get_service_detail(
            tenant_code=tenant.code,
            service_code=data.service_code
        )

        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Service '{data.service_code}' not found for tenant '{tenant.code}'"
            )

        return result

    except HTTPException:
        raise
    except ServiceValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update-owner", response_model=UpdateServiceOwnerResponse, summary="Update Service Owner")
async def update_service_owner(
    data: UpdateServiceOwnerRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Change the owner (point of contact) of a service.

    Security:
        - JWT authentication required; tenant isolation enforced.
        - Allowed for org owners, or users with a permission on the service.

    Request Body:
        - service_code: Service to update
        - owner_user_code: user_mst.code of the new owner, or null to unassign
    """
    try:
        user, tenant = user_and_tenant

        # Permission: org owner bypasses; otherwise must have access to the service.
        perm_helper = PermissionHelper(db)
        if not await perm_helper.is_org_owner(user):
            perm_service = PermissionService(db)
            has_permission = await perm_service.has_any_service_permission(
                user_mst_code=user.code,
                tenants_mst_code=tenant.code,
                service_mst_code=data.service_code
            )
            if not has_permission:
                raise HTTPException(
                    status_code=403,
                    detail=f"You don't have permission to modify service '{data.service_code}'"
                )

        service = ServicesMstService(db)
        result = await service.update_service_owner(
            tenant_code=tenant.code,
            service_code=data.service_code,
            owner_user_code=data.owner_user_code,
        )

        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Service '{data.service_code}' not found for tenant '{tenant.code}'"
            )

        return {**result, 'message': 'Service owner updated successfully'}

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ServiceValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/delete-service", response_model=DeleteServiceResponse, summary="Delete Service")
async def delete_service(
    data: DeleteServiceRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Soft delete a service by service code.

    Sets is_deleted=True and is_active=False. The record is NOT removed from the database.

    Request Body:
        - service_code: Code of the service to delete

    Raises:
        404: Service not found or doesn't belong to tenant
    """
    try:
        user, tenant = user_and_tenant

        service = ServicesMstService(db)
        result = await service.delete_service(
            tenant_code=tenant.code,
            service_code=data.service_code,
        )

        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Service '{data.service_code}' not found for tenant '{tenant.code}'"
            )

        return result

    except HTTPException:
        raise
    except ServiceValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create-service", response_model=CreateServiceResponse, summary="Create Service")
async def create_service(
    data: CreateServiceRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new service with auto-generated service code.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: service created under authenticated user's tenant
        - tenant_code automatically injected from JWT token
        - Validates application belongs to tenant (via DB constraint)
        - Validates resource group exists (via DB constraint)

    Request Body:
        - application_code: Application code (must belong to tenant)
        - resource_group_code: Resource group code (must exist)
        - service_name: Name of the service (1-255 characters)
        - service_type: Type of service (API or Background Service)
        - description: Optional description (max 500 characters)
        - is_active: Active status (default: true)

    Response:
        - id: Database ID
        - service_code: Auto-generated UUID service code
        - service_name: Service name
        - description: Service description
        - tenant_code: Tenant code
        - application_code: Application code
        - resource_group_code: Resource group code
        - service_type: Service type (API or Background Service)
        - is_active: Active status
        - created_at: Timestamp
        - message: Success message

    Returns:
        CreateServiceResponse with created service details

    Raises:
        400: Validation error (invalid fields)
        422: Validation error (missing or malformed fields)
        500: Database constraint violation (application/resource_group not found) or internal error

    Examples:
        POST /api/v1/services/create-service
        Body: {
            "application_code": "ecommerce_app",
            "resource_group_code": "prod_rg",
            "service_name": "Payment API",
            "service_type": "API",
            "description": "Handles payment processing",
            "is_active": true
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Initialize service layer
        service = ServicesMstService(db)

        # Call service layer with tenant_code from JWT (tenant isolation enforced)
        # tenant_code is NOT from request - it's from authenticated user's JWT token
        result = await service.create_service(tenant_code=tenant.code, user_code=user.code, data=data)

        return result

    except HTTPException:
        raise
    except ServiceValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        # Database constraint violations (application/resource_group not found)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
