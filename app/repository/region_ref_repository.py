"""
Repository for RegionRef operations
"""
from typing import List, Optional
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.region_ref_model import RegionRefModel
from app.repository.base_repository import BaseRepository


class RegionRefRepository(BaseRepository[RegionRefModel]):
    """Repository for Region Reference operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(RegionRefModel, session)

    async def get_by_code(self, code: str) -> Optional[RegionRefModel]:
        """
        Get a region by its code

        Args:
            code: The region code

        Returns:
            RegionRefModel instance or None if not found
        """
        return await self.get_by(code=code, is_active=True, is_deleted=False)

    async def get_regions_by_vendor(
        self,
        vendor: str,
        active_only: bool = True
    ) -> List[RegionRefModel]:
        """
        Get all regions for a specific vendor (for cascading dropdown)

        Args:
            vendor: Infrastructure vendor ('aws', 'azure', 'gcp', 'on_prem')
            active_only: If True, only return active regions (default: True)

        Returns:
            List of RegionRefModel instances ordered by display_order
        """
        filters = [
            self.model.infra_vendor_enum == vendor
        ]

        if active_only:
            filters.extend([
                self.model.is_active == True,
                self.model.is_deleted == False
            ])

        # Build query with multiple order_by columns
        stmt = select(self.model)
        if filters:
            stmt = stmt.where(and_(*filters))
        stmt = stmt.order_by(self.model.display_order, self.model.name)

        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def exists_by_vendor_region(
        self,
        vendor: str,
        region_identifier: str
    ) -> bool:
        """
        Check if a vendor-region combination exists (for validation)

        Args:
            vendor: Infrastructure vendor
            region_identifier: Region identifier (e.g., 'us-east-1')

        Returns:
            True if exists and active, False otherwise
        """
        return await self.exists(
            infra_vendor_enum=vendor,
            region_identifier=region_identifier,
            is_active=True,
            is_deleted=False
        )
