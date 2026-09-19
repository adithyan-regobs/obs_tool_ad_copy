"""
Kong Route Ops API Endpoints
"""
import logging
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.enum import EnvironmentEnum
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.repository.kong_route_configs_repository import KongRouteConfigsRepository
from app.repository.service_config_repository import ServiceConfigRepository
from app.schemas.dropdown_option_schemas import DropdownOption
from app.schemas.validator_response_schemas import ValidationResult
from app.services.kong_ops.kong_route_ops import KongRouteOps

logger = logging.getLogger(__name__)
router = APIRouter()


class ListPublicFacingServicesRequest(BaseModel):
    product_code: str = Field(..., description="Application/product code")
    environment: EnvironmentEnum = Field(..., description="Environment (dev/staging/prod)")
    geo_loc: str = Field(..., description="Geographic location code")


class ValidateDuplicateRouteRequest(BaseModel):
    api_name: str = Field(..., description="Kong API identifier")
    http_method: str = Field(..., description="HTTP method (GET, POST, etc.)")
    route_path: str = Field(..., description="Kong route path/pattern")
    services_code: Optional[str] = Field(
        None, description="Optional service code for service-scoped routes"
    )


@router.post(
    "/services",
    response_model=List[DropdownOption],
    summary="List public-facing API services for a product/env/geo",
)
async def list_public_facing_services(
    payload: ListPublicFacingServicesRequest,
    page: Optional[int] = Query(None, ge=1, description="1-indexed page number; omit for all results"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> List[DropdownOption]:
    """
    Return all public-facing API services for the given
    (product_code, environment, geo_loc) combination.

    If `page` query param is provided, results are paginated; otherwise all
    matching services are returned.
    """
    _, tenant = user_and_tenant
    kong_repo = KongRouteConfigsRepository(db)
    service_config_repo = ServiceConfigRepository(db)
    ops = KongRouteOps(
        kong_route_repo=kong_repo,
        service_config_repo=service_config_repo,
    )
    return await ops.get_services(
        tenant_code=tenant.code,
        product_code=payload.product_code,
        environment=payload.environment,
        geo_loc=payload.geo_loc,
        page=page,
    )


@router.post(
    "/validate-duplicate-route",
    response_model=ValidationResult,
    summary="Validate if a Kong route already exists",
)
async def validate_duplicate_route(
    payload: ValidateDuplicateRouteRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> ValidationResult:
    """
    Check whether a Kong route with the given (api_name, http_method, route_path)
    — and optionally services_code — already exists.

    Returns a `ValidationResult` — the standard validator response format.
    See `app.schemas.validator_response_schemas.ValidationResult`.
    """
    user, tenant = user_and_tenant

    logger.info(
        "VALIDATE DUPLICATE KONG ROUTE - request received",
        extra={
            "user_code": user.code,
            "user_email": user.email_id,
            "tenant_code": tenant.code,
            "tenant_subdomain": tenant.subdomain,
            "api_name": payload.api_name,
            "http_method": payload.http_method,
            "route_path": payload.route_path,
            "services_code": payload.services_code,
            "endpoint": "/kong-ops/routes/validate-duplicate-route",
        },
    )

    repo = KongRouteConfigsRepository(db)
    ops = KongRouteOps(kong_route_repo=repo)
    result = await ops.duplicate_route_validator(
        tenant_code=tenant.code,
        api_name=payload.api_name,
        http_method=payload.http_method,
        route_path=payload.route_path,
        services_code=payload.services_code,
    )

    logger.info(
        "VALIDATE DUPLICATE KONG ROUTE - result",
        extra={
            "tenant_code": tenant.code,
            "api_name": payload.api_name,
            "http_method": payload.http_method,
            "route_path": payload.route_path,
            "services_code": payload.services_code,
            "valid": result.valid,
            "description": result.description,
        },
    )
    return result
