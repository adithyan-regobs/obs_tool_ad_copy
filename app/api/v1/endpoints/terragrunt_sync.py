"""
Terragrunt Sync API Endpoints

Handles syncing service configurations to local Terragrunt HCL files.
"""

from typing import Tuple

from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.services.terragrunt_sync_service import TerragruntSyncService
from app.repository.service_config_repository import ServiceConfigRepository
from app.repository.services_mst_repository import ServicesMstRepository
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/sync/{service_code}/{environment}",
    summary="Sync Service Config to Terragrunt HCL",
    status_code=status.HTTP_200_OK
)
async def sync_service_config_to_terragrunt(
    service_code: str,
    environment: str,
    db: AsyncSession = Depends(get_db),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Manually trigger Terragrunt HCL file sync for a service configuration.

    This endpoint:
    1. Fetches the service configuration from database
    2. Gets the service name from services_mst table
    3. Creates/updates the Terragrunt HCL file locally
    4. Updates field values based on database config

    **Path Parameters:**
    - service_code: Service code from services_mst (e.g., "service-casa")
    - environment: Environment name (dev/staging/prod)

    **Returns:**
    - status: "success" or "error"
    - file_path: Relative path to the generated HCL file
    - message: Description of the result

    **Errors:**
    - 404: Service or service configuration not found
    - 500: Internal server error during sync

    **Example:**
    ```
    POST /api/v1/terragrunt-sync/sync/service-casa/dev
    ```

    **Response:**
    ```json
    {
      "status": "success",
      "file_path": "templates/terragrunt/services/casa/existing-alb.hcl",
      "message": "Terragrunt HCL file synced successfully"
    }
    ```
    """
    try:
        # Extract user and tenant from auth token
        user, tenant = user_and_tenant
        tenant_code = tenant.code
        user_email = user.email_id

        logger.info(f"Starting manual Terragrunt sync for {service_code} in {environment} (tenant: {tenant_code}, user: {user_email})")

        # Get service config
        config_repo = ServiceConfigRepository(db)
        service_config = await config_repo.get_by_service_and_env(service_code, environment)

        if not service_config:
            logger.warning(f"Service configuration not found: {service_code} in {environment}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Service configuration not found for {service_code} in {environment}"
            )

        # Get service with relationships loaded (needed for sync)
        services_repo = ServicesMstRepository(db)
        service = await services_repo.get_by_code(service_code)

        if not service:
            logger.warning(f"Service not found: {service_code}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Service not found: {service_code}"
            )

        # Trigger sync
        terragrunt_service = TerragruntSyncService()
        result = await terragrunt_service.sync_config_to_hcl(
            service_config,
            service,
            tenant=tenant_code,
            user_email=user_email
        )

        if result["status"] == "error":
            logger.error(f"Terragrunt sync failed: {result['message']}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result["message"]
            )

        logger.info(f"Terragrunt sync successful: {result['file_path']}")
        return result

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        # Unexpected errors
        logger.error(f"Unexpected error in sync endpoint: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terragrunt sync failed: {str(e)}"
        )
