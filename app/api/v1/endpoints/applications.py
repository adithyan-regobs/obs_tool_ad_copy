from typing import List, Tuple
import logging
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.applications_mst_service import ApplicationsMstService
from app.domain.validators.applications_mst_rules import ApplicationValidationError
from app.schemas.application_schemas import (
    GetAllApplicationsRequest,
    ApplicationsListResponse,
    CreateApplicationRequest,
    ApplicationCreateResponse
)
from app.schemas.dropdown_option_schemas import DropdownOption
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/get-all-applications", response_model=ApplicationsListResponse, summary="Get All Applications")
async def get_all_applications(
    data: GetAllApplicationsRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all applications with complete information including tenant, services count,
    resource groups count, alerts count, and infrastructure count.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: only returns applications for authenticated user's tenant
        - tenant_code automatically injected from JWT token

    Request Body:
        - is_active (optional): Filter by status (true/false)
        - skip (optional): Pagination offset (default: 0)
        - limit (optional): Page size (default: 100, max: 500)

    Response:
        - total: Total number of matching applications
        - skip: Pagination offset used
        - limit: Page size used
        - applications: List of application details with:
            - Basic info (id, code, name, description, status)
            - Tenant relationship (tenant_code, tenant_name)
            - Aggregated counts (services, resource groups, alerts, infrastructure)
            - Timestamps (created_at, updated_at)

    Examples:
        # Get all applications
        POST /api/v1/applications/get-all-applications
        Body: {}

        # Get only active applications
        POST /api/v1/applications/get-all-applications
        Body: {"is_active": true}

        # With pagination
        POST /api/v1/applications/get-all-applications
        Body: {"skip": 0, "limit": 20}
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log tenant isolation check
        logger.info(
            "GET ALL APPLICATIONS - Tenant isolation check",
            extra={
                "user_code": user.code,
                "user_email": user.email_id,
                "tenant_code": tenant.code,
                "tenant_name": tenant.name,
                "tenant_subdomain": tenant.subdomain,
                "endpoint": "/get-all-applications"
            }
        )
        logger.debug(
            f"Request parameters: is_active={data.is_active}, skip={data.skip}, limit={data.limit}"
        )

        # Initialize service layer
        service = ApplicationsMstService(db)

        # Call service layer with tenant_code from JWT (tenant isolation enforced)
        result = await service.get_all_applications(
            tenant_code=tenant.code,
            user_code=user.code,
            is_active=data.is_active,
            skip=data.skip,
            limit=data.limit,
            workspace_code=data.workspace_code,
        )

        logger.info(
            f"Successfully retrieved {result['total']} applications for tenant {tenant.code}"
        )
        return {
            "total": result["total"],
            "skip": data.skip,
            "limit": data.limit,
            "applications": result["applications"]
        }
    except HTTPException:
        raise
    except ApplicationValidationError as e:
        logger.warning(f"Validation error in get_all_applications: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(
            f"Unexpected error in get_all_applications: {str(e)}",
            exc_info=True,
            extra={"tenant_code": tenant.code if 'tenant' in locals() else None}
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/get-applications-dropdown",
    response_model=List[DropdownOption],
    summary="Get Applications as Dropdown Options",
)
async def get_applications_dropdown(
    data: GetAllApplicationsRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> List[DropdownOption]:
    """
    Same functionality as `/get-all-applications` but returns the standardized
    `[{label, value}]` dropdown shape where `value` is the application code
    and `label` is the application name.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: only returns applications for authenticated user's tenant
        - tenant_code automatically injected from JWT token

    Request Body:
        - is_active (optional): Filter by status (true/false)
        - skip (optional): Pagination offset (default: 0)
        - limit (optional): Page size (default: 100, max: 500)

    Response:
        List of `{label, value}` dropdown options.

    Examples:
        # All applications
        POST /api/v1/applications/get-applications-dropdown
        Body: {}

        # Only active applications
        POST /api/v1/applications/get-applications-dropdown
        Body: {"is_active": true}
    """
    try:
        user, tenant = user_and_tenant

        logger.info(
            "GET APPLICATIONS DROPDOWN - Tenant isolation check",
            extra={
                "user_code": user.code,
                "user_email": user.email_id,
                "tenant_code": tenant.code,
                "tenant_name": tenant.name,
                "tenant_subdomain": tenant.subdomain,
                "endpoint": "/get-applications-dropdown",
            },
        )
        logger.debug(
            f"Request parameters: is_active={data.is_active}, skip={data.skip}, limit={data.limit}"
        )

        service = ApplicationsMstService(db)
        return await service.get_applications_dropdown(
            tenant_code=tenant.code,
            user_code=user.code,
            is_active=data.is_active,
            skip=data.skip,
            limit=data.limit,
            workspace_code=data.workspace_code,
        )
    except HTTPException:
        raise
    except ApplicationValidationError as e:
        logger.warning(f"Validation error in get_applications_dropdown: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(
            f"Unexpected error in get_applications_dropdown: {str(e)}",
            exc_info=True,
            extra={"tenant_code": tenant.code if 'tenant' in locals() else None},
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create-application", response_model=ApplicationCreateResponse, summary="Create Application")
async def create_application(
    data: CreateApplicationRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new application with auto-generated default resource group.

    This endpoint performs the following operations atomically:
    1. Creates application with auto-generated UUID code under authenticated user's tenant
    2. Auto-creates default resource group with dummy data

    Security:
        - JWT authentication required
        - Tenant isolation enforced: application created under authenticated user's tenant
        - tenant subdomain automatically injected from JWT token

    Request Body:
        - application_name (required): Application name (1-255 characters)
        - description (optional): Application description (max 500 characters)

    Response:
        - Application details (id, code, name, description, tenant info, timestamps)
        - Default resource group details (code, name)
        - Success message

    Raises:
        400: Validation error
        500: Internal server error

    Examples:
        # Create application
        POST /api/v1/applications/create-application
        Body: {
            "application_name": "Payment Service",
            "description": "Payment processing application"
        }

        Response: {
            "id": 42,
            "code": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "name": "Payment Service",
            "description": "Payment processing application",
            "tenant_code": "550e8400-e29b-41d4-a716-446655440000",
            "tenant_name": "Acme Corp",
            "is_active": true,
            "created_at": "2025-01-15T14:30:00Z",
            "default_resource_group_code": "f7e8d9c0-1234-5678-90ab-cdef12345678",
            "default_resource_group_name": "Payment Service - Default",
            "message": "Application created successfully with default resource group"
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log tenant isolation check
        logger.info(
            "CREATE APPLICATION - Tenant isolation check",
            extra={
                "user_code": user.code,
                "user_email": user.email_id,
                "tenant_code": tenant.code,
                "tenant_name": tenant.name,
                "tenant_subdomain": tenant.subdomain,
                "application_name": data.application_name,
                "endpoint": "/create-application"
            }
        )
        logger.debug(
            f"Application details: name={data.application_name}, description={data.description}"
        )

        # Initialize service
        service = ApplicationsMstService(db)

        # Call service with tenant subdomain from JWT (tenant isolation enforced)
        result = await service.create_application(tenant_subdomain=tenant.subdomain, data=data, user_code=user.code)

        logger.info(
            f"Successfully created application '{data.application_name}' for tenant {tenant.code}",
            extra={"application_code": result.get("code")}
        )
        return result
    except HTTPException:
        raise
    except ApplicationValidationError as e:
        logger.warning(f"Validation error in create_application: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        # Tenant not found or other business logic errors
        logger.warning(f"Business logic error in create_application: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(
            f"Unexpected error in create_application: {str(e)}",
            exc_info=True,
            extra={
                "tenant_code": tenant.code if 'tenant' in locals() else None,
                "application_name": data.application_name if data else None
            }
        )
        raise HTTPException(status_code=500, detail=str(e))
