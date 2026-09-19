"""
Geographic Location Master Service
Business logic for geographic location master operations
"""
from typing import Any, Dict, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.geo_loc_mst_repository import GeoLocMstRepository


class GeoLocMstService:
    """Service for geographic location master operations"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = GeoLocMstRepository(session)

    async def get_geo_locs_by_tenant(self, tenant_code: str) -> Dict[str, Any]:
        """
        Get all active geographic locations for a tenant.

        Args:
            tenant_code: Tenant code

        Returns:
            Dict with total count and list of geographic locations
        """
        geo_locs = await self.repository.get_by_tenant(tenant_code)
        return {
            "total": len(geo_locs),
            "geo_locs": [{"code": g.code, "name": g.name} for g in geo_locs]
        }

    async def get_geo_locs_dropdown(self, tenant_code: str) -> List[Dict[str, str]]:
        """
        Get all active geographic locations for a tenant as `[{label, value}]`
        dropdown options where `value` is the geo-loc code and `label` is the
        geo-loc name.

        Calls the same repository method as `get_geo_locs_by_tenant`, then
        transforms the rows into the standardized dropdown shape used across
        the app.

        Args:
            tenant_code: Tenant code

        Returns:
            List of `{label, value}` dropdown options
        """
        geo_locs = await self.repository.get_by_tenant(tenant_code)
        return [{"label": g.name, "value": g.code} for g in geo_locs]
