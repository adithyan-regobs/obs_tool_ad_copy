"""
Authentication API endpoint
"""
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Tuple
import logging

from app.services.auth_service import AuthService
from app.services.permission_cache_service import PermissionCacheService
from app.schemas.auth_schemas import LoginResponse, SubdomainValidationResponse
from app.api.dependencies import get_db, get_current_user_and_tenant
from fastapi import Query
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.utils.role_lookup_helper import get_user_role_type_codes

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/login", response_model=LoginResponse, summary="User Login")
async def login(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    User login endpoint - validates JWT and returns user + tenant information.

    This endpoint:
    1. Validates JWT token from Authorization header
    2. Extracts userId and organizationSubdomain from JWT claims
    3. Verifies user exists in database and is active
    4. Verifies tenant exists in database and is active
    5. Confirms user belongs to the tenant
    6. Returns complete user and tenant information

    **Authentication:**
    - Requires Bearer token in Authorization header
    - Token must be valid Clerk JWT

    **Request Headers:**
    - Authorization: Bearer <clerk_jwt_token>

    **Response:**
    - user_id: Internal user UUID
    - user_code: User code
    - first_name: User's first name
    - last_name: User's last name
    - email: User's email address
    - is_org_owner: Whether user owns the organization
    - tenant_id: Internal tenant UUID
    - tenant_code: Tenant/organization code
    - tenant_name: Organization name
    - tenant_subdomain: Organization subdomain from Clerk
    - auth_provider: Authentication provider (clerk, manual, etc.)

    **Errors:**
    - 401: Invalid or missing authentication token
    - 403: User not found, tenant not found, or user doesn't belong to tenant
    - 500: Internal server error

    **Example:**
    ```
    POST /api/v1/auth/login
    Headers:
      Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...
    ```
    """
    try:
        user, tenant = user_and_tenant

        # The get_current_user_and_tenant dependency already validated everything
        # Now we just return the user and tenant information

        # Auto-rebuild permission cache on login
        try:
            logger.info(f"[CACHE] Starting permission cache rebuild for user_id={user.id}, user_code={user.code}")

            role_type_codes = await get_user_role_type_codes(db, user.id)
            logger.info(f"[CACHE] Got role_type_codes: {role_type_codes}")

            cache_service = PermissionCacheService(db)
            logger.info(f"[CACHE] Calling rebuild_cache with user_mst_code={user.code}, tenant={tenant.code}")

            result = await cache_service.rebuild_cache(
                user_mst_code=user.code,
                tenants_mst_code=tenant.code,
                role_type_codes=role_type_codes
            )
            logger.info(f"[CACHE] Cache rebuild result: {result}")
            logger.info(f"Permission cache rebuilt for user: {user.code}")
        except Exception as cache_error:
            # Log but don't fail login if cache rebuild fails
            import traceback
            logger.error(f"[CACHE] Failed to rebuild permission cache: {str(cache_error)}")
            logger.error(f"[CACHE] Traceback: {traceback.format_exc()}")

        logger.info(f"Login successful for user: {user.email_id} (tenant: {tenant.name})")

        return LoginResponse(
            user_id=str(user.id),
            user_code=user.code,
            first_name=user.first_name,
            last_name=user.last_name,
            email=user.email_id,
            is_org_owner=user.is_org_owner,
            tenant_id=str(tenant.id),
            tenant_code=tenant.code,
            tenant_name=tenant.name,
            tenant_subdomain=tenant.subdomain,
            auth_provider=user.auth_provider.value if user.auth_provider else "manual"
        )


    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")


@router.get("/validate-subdomain", response_model=SubdomainValidationResponse, summary="Validate Subdomain Availability")
async def validate_subdomain(
    subdomain: str = Query(..., min_length=1, max_length=63, description="Subdomain to validate"),
    db: AsyncSession = Depends(get_db)
):
    """
    Validate if a subdomain is available for organization creation.

    This endpoint checks if a subdomain is already taken by an existing tenant/organization.
    It's used during organization signup to provide real-time feedback to users.

    **Query Parameters:**
    - `subdomain` (required): The subdomain to validate (1-63 characters)

    **Response:**
    ```json
    {
        "available": true,
        "subdomain": "myorg",
        "message": "Subdomain 'myorg' is available"
    }
    ```

    **Use Case:**
    - Frontend calls this during organization creation form to validate subdomain in real-time
    - Provides immediate feedback to users about subdomain availability

    **Returns:**
    - `available`: Boolean indicating if subdomain is available
    - `subdomain`: The subdomain that was checked
    - `message`: Human-readable message about availability

    **Raises:**
    - `400`: Invalid subdomain format (empty or invalid characters)
    - `500`: Internal server error

    **Example:**
    ```
    GET /api/v1/auth/validate-subdomain?subdomain=myorg
    ```
    """
    try:
        service = AuthService(db)
        result = await service.check_subdomain_availability(subdomain)

        return SubdomainValidationResponse(
            available=result["available"],
            subdomain=result["subdomain"],
            message=result["message"]
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Subdomain validation error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Validation failed: {str(e)}")
