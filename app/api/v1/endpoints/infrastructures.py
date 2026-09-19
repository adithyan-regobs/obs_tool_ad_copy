"""
Infrastructure Creation API Endpoints

Unified API endpoints for creating infrastructure resources and Kong routes.
"""
import logging
from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.authz.security import AuthenticationOnly, SecureRouter
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.schemas.infrastructure_schemas import InfrastructureCreateRequest, InfrastructureCreateResponse
from app.services.infrastructure_creation_service import InfrastructureCreationService

logger = logging.getLogger(__name__)

router = SecureRouter()


@router.post(
    "",
    response_model=InfrastructureCreateResponse,
    status_code=status.HTTP_201_CREATED,
    access=AuthenticationOnly(
        reason="tenant-scoped create/update; object-level card pending typed routes"
    ),
)
async def create_infrastructure_resource(
    request: InfrastructureCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Unified endpoint for creating or updating infrastructure resources and Kong routes.

    Supports UPSERT:
    - If request.code is provided: Updates existing record
    - If request.code is None: Creates new record

    Routes to appropriate table based on infrastructuretype_ref_code:
    - kong_infrastructuretype_ref → Kong Gateway routes in kong_route_configs table
    - Any other type → S3, SQS, DynamoDB, ECS, Lambda, etc. in infrastructure_mst table

    Args:
        request: Infrastructure creation/update request
        db: Database session
        current_user_tenant: Current user and tenant from JWT

    Returns:
        InfrastructureCreateResponse with table_name and generated code

    Raises:
        HTTPException 400: Invalid infrastructuretype_ref_code, validation errors, or duplicate resources (for create)
        HTTPException 403: Tenant isolation violation (service/infrastructure belongs to different tenant)
        HTTPException 404: Infrastructure type not found, service not found, or record not found (for update)
        HTTPException 500: Internal server error

    Examples:
        Create S3 Bucket:
        POST /api/v1/infrastructures
        {
            "infrastructuretype_ref_code": "s3_infrastructuretype_ref",
            "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
            "environment": "dev",
            "geo_loc_mst_code": "region-aspora-mumbai",
            "type_specific_config": {
                "identifier": "my-app-logs-bucket",
                "region": "ap-south-1"
            }
        }

        Update S3 Bucket:
        POST /api/v1/infrastructures
        {
            "code": "INFRA_S3_ABC123",
            "infrastructuretype_ref_code": "s3_infrastructuretype_ref",
            "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
            "environment": "dev",
            "geo_loc_mst_code": "region-aspora-mumbai",
            "type_specific_config": {
                "identifier": "my-updated-bucket",
                "region": "ap-south-1"
            }
        }

        Create Kong Route:
        POST /api/v1/infrastructures
        {
            "infrastructuretype_ref_code": "kong_infrastructuretype_ref",
            "services_mst_code": "f7513b9e-c6ab-4a8e-a61b-b1afeb604829",
            "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
            "environment": "dev",
            "geo_loc_mst_code": "region-aspora-mumbai",
            "type_specific_config": {
                "api_name": "user-api",
                "http_method": "GET",
                "route_path": "~/api/v1/users$"
            }
        }

        Update Kong Route:
        POST /api/v1/infrastructures
        {
            "code": "KRC_DEF12345",
            "infrastructuretype_ref_code": "kong_infrastructuretype_ref",
            "services_mst_code": "f7513b9e-c6ab-4a8e-a61b-b1afeb604829",
            "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
            "environment": "dev",
            "geo_loc_mst_code": "region-aspora-mumbai",
            "type_specific_config": {
                "api_name": "user-api",
                "http_method": "POST",
                "route_path": "~/api/v1/users$"
            }
        }
    """
    user, tenant = current_user_tenant

    is_update = request.code is not None
    logger.info(
        f"{'Updating' if is_update else 'Creating'} infrastructure resource: "
        f"infrastructuretype_ref_code={request.infrastructuretype_ref_code}, "
        f"code={request.code}, "
        f"tenant={tenant.code}, user={user.email_id}"
    )

    service = InfrastructureCreationService(db)

    try:
        # Config validation now lives in InfrastructureCreationService, which the
        # Slack handlers and the MCP dispatcher also call — this route used to
        # hold the DynamoDB rules alone, so those callers had none.
        result = await service.create_resource(
            tenant_code=tenant.code,
            user_code=user.code,
            request=request,
            user_email=user.email_id
        )

        logger.info(
            f"Successfully {'updated' if is_update else 'created'} resource: table={result.table_name}, code={result.code}"
        )

        return result

    except ValueError as e:
        # Validation errors (invalid infrastructuretype_ref_code, missing fields, etc.)
        logger.warning(f"Validation error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        # Re-raise HTTPExceptions from service layer (403, 404, 400)
        raise
    except Exception as e:
        # Unexpected errors
        logger.error(f"Failed to {'update' if is_update else 'create'} infrastructure resource: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error occurred while {'updating' if is_update else 'creating'} infrastructure resource"
        )
