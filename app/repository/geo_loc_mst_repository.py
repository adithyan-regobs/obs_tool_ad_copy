"""
Geographic Location Master Repository
Handles data access operations for geo_loc_mst table
"""
from typing import List, Optional
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.geo_loc_mst_model import GeoLocMstModel
from app.repository.base_repository import BaseRepository


class GeoLocMstRepository(BaseRepository[GeoLocMstModel]):
    """Repository for GeoLocMst operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(GeoLocMstModel, session)

    async def get_by_tenant(self, tenant_code: str) -> List[GeoLocMstModel]:
        """
        Get all active geographic locations for a specific tenant.

        Args:
            tenant_code: Tenant code to filter locations

        Returns:
            List of active geographic locations for the tenant
        """
        stmt = select(self.model).where(
            and_(
                self.model.tenants_mst_code == tenant_code,
                self.model.is_active == True,
                self.model.is_deleted == False
            )
        ).order_by(self.model.name)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_code(self, code: str) -> Optional[GeoLocMstModel]:
        """
        Get geographic location by code.

        Args:
            code: Geographic location code

        Returns:
            GeoLocMstModel if found, None otherwise
        """
        return await self.get_by(code=code)
