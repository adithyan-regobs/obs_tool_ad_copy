"""
Service Reference Tools API Endpoints

REST API endpoints for MCP server consumption.
"""
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Tuple
import logging

from app.services.service_reference_tools_service import ServiceReferenceToolsService
from app.schemas.service_reference_tools_schemas import (
    ListServicesRequestSchema,
    ListServicesResponseSchema,
    ServiceItemSchema,
)
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/list-services",
    response_model=ListServicesResponseSchema,
    summary="List Services with Configs",
)
async def list_services(
    request: ListServicesRequestSchema,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    List all services that have configurations.

    Designed for MCP server consumption.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Request Body:
        - environment: Environment enum (dev, staging, prod) - required
        - geo_loc_code: Geographic location code - required
        - infra_vendor: Infrastructure vendor (optional)
        - infrastructure_type: Infrastructure type (optional)

    Response:
        - services: List of services with configs
        - count: Total number of services
        - infra_suffix: Formatted infrastructure context string

    Example:
        POST /api/v1/service-reference-tools/list-services
        Body: {
            "environment": "staging",
            "geo_loc_code": "mumbai"
        }
    """
    try:
        user, tenant = user_and_tenant

        logger.info(
            "SERVICE REFERENCE TOOLS API - list_services",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "environment": request.environment.value,
                "geo_loc_code": request.geo_loc_code,
            }
        )

        # Initialize service
        service = ServiceReferenceToolsService(db)

        # Call service layer
        result = await service.list_services(
            tenant_code=tenant.code,
            environment=request.environment,
            geo_loc_code=request.geo_loc_code,
            infra_vendor=request.infra_vendor,
            infrastructure_type=request.infrastructure_type,
        )

        # Transform to response schema
        services = [ServiceItemSchema(**svc) for svc in result["services"]]

        logger.info(
            "SERVICE REFERENCE TOOLS API - response",
            extra={"count": result["count"]}
        )

        return ListServicesResponseSchema(
            services=services,
            count=result["count"],
            infra_suffix=result["infra_suffix"],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"SERVICE REFERENCE TOOLS API - Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/test-list-services",
    response_model=ListServicesResponseSchema,
    summary="Test List Services (No Auth)",
    tags=["Test - Service Reference Tools"]
)
async def test_list_services(
    request: ListServicesRequestSchema,
    db: AsyncSession = Depends(get_db),
):
    """
    Test endpoint for list_services WITHOUT authentication.

    Uses hardcoded tenant 'aspora' for testing.
    This is for testing purposes only.

    Example:
        POST /api/v1/service-reference-tools/test-list-services
        Body: {
            "environment": "staging",
            "geo_loc_code": "mumbai"
        }
    """
    try:
        tenant_code = "vance"  # Hardcoded for testing

        logger.info(
            "[TEST] SERVICE REFERENCE TOOLS API - list_services",
            extra={
                "tenant_code": tenant_code,
                "environment": request.environment.value,
                "geo_loc_code": request.geo_loc_code,
            }
        )

        service = ServiceReferenceToolsService(db)
        result = await service.list_services(
            tenant_code=tenant_code,
            environment=request.environment,
            geo_loc_code=request.geo_loc_code,
            infra_vendor=request.infra_vendor,
            infrastructure_type=request.infrastructure_type,
        )

        services = [ServiceItemSchema(**svc) for svc in result["services"]]

        logger.info(
            "[TEST] SERVICE REFERENCE TOOLS API - response",
            extra={"count": result["count"]}
        )

        return ListServicesResponseSchema(
            services=services,
            count=result["count"],
            infra_suffix=result["infra_suffix"],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[TEST] SERVICE REFERENCE TOOLS API - Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
