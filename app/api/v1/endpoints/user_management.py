"""
User Management API Endpoints.

Provides endpoints for user lookup and management operations.
"""
from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.api.dependencies import get_db
from app.services.user_management_service import UserManagementService
from app.schemas.user_management_schemas import (
    GetUserByEmailRequest,
    GetUserByEmailResponse
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/get-user-by-email",
    response_model=GetUserByEmailResponse,
    summary="Get User Details by Email",
    status_code=status.HTTP_200_OK
)
async def get_user_by_email(
    request: GetUserByEmailRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Get user details by email address.

    This endpoint is used to map external user identifiers (like Slack user IDs)
    to internal user codes and tenant codes.

    **Use Case:**
    - Slack integration: Map Slack user email to DevLift user
    - External integrations: Lookup user details by email

    **Security:**
    - This endpoint does NOT require JWT authentication (used by integrations)
    - Returns only basic user information (no sensitive data)

    **Request Body:**
    - `email`: User email address (required)

    **Response:**
    - `user_mst_code`: User code
    - `tenants_mst_code`: Tenant code
    - `email_id`: User email address
    - `first_name`: User first name
    - `last_name`: User last name
    - `is_org_owner`: Whether user is organization owner

    **Errors:**
    - 404: User not found with provided email
    - 500: Internal server error

    **Example Request:**
    ```json
    POST /api/v1/user-management/get-user-by-email
    {
        "email": "john.doe@example.com"
    }
    ```

    **Example Response:**
    ```json
    {
        "user_mst_code": "user-abc123",
        "tenants_mst_code": "tenant-xyz",
        "email_id": "john.doe@example.com",
        "first_name": "John",
        "last_name": "Doe",
        "is_org_owner": false
    }
    ```
    """
    try:
        # Initialize service
        service = UserManagementService(db)

        # Fetch user by email
        user = await service.get_user_by_email(request.email)

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No user found with email: {request.email}"
            )

        # Build response
        return GetUserByEmailResponse(
            user_mst_code=user.code,
            tenants_mst_code=user.tenants_mst_code,
            email_id=user.email_id,
            first_name=user.first_name,
            last_name=user.last_name,
            is_org_owner=user.is_org_owner
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to fetch user by email {request.email}: {str(e)}",
            exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user details: {str(e)}"
        )
