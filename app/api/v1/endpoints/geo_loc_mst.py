"""
Geographic Location Master API Endpoints
API routes for geographic location master operations
"""
from typing import List, Tuple
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.schemas.dropdown_option_schemas import DropdownOption
from app.schemas.geo_loc_mst_schemas import GeoLocMstListResponse
from app.services.geo_loc_mst_service import GeoLocMstService


router = APIRouter(prefix="/geo-loc-mst", tags=["Geographic Location Master"])


@router.get(
    "",
    response_model=GeoLocMstListResponse,
    summary="Get Geographic Locations for Tenant",
    description="Get all active business/deployment geographic locations for the current tenant"
)
async def get_geo_locs(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> GeoLocMstListResponse:
    """
    Get all active geographic locations for the current tenant.

    These are business/deployment geographic locations (e.g., US, UK, Bahrain),
    NOT AWS regions.
    """
    user, tenant = user_and_tenant
    service = GeoLocMstService(db)
    return await service.get_geo_locs_by_tenant(tenant.code)


@router.post(
    "/get-geo-locs-dropdown",
    response_model=List[DropdownOption],
    summary="Get Geographic Locations as Dropdown Options",
)
async def get_geo_locs_dropdown(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> List[DropdownOption]:
    """
    Same functionality as `GET /geo-loc-mst` but returns the standardized
    `[{label, value}]` dropdown shape where `value` is the geo-loc code and
    `label` is the geo-loc name.

    Security:
        - JWT authentication required
        - Tenant isolation enforced via JWT
    """
    _, tenant = user_and_tenant
    service = GeoLocMstService(db)
    return await service.get_geo_locs_dropdown(tenant.code)
