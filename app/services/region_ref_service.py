"""
Service layer for Region Reference operations
"""
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.region_ref_repository import RegionRefRepository
from app.domain.factories.region_ref_factory import make_regions_list_response


class RegionRefService:
    """
    Service layer for Region Reference operations.
    Handles business logic for region reference data.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.region_repository = RegionRefRepository(session)

    async def get_regions_by_vendor(self, vendor: str) -> Dict[str, Any]:
        """
        Get all regions for a specific infrastructure vendor.

        Business Logic:
        - Returns all active regions for the specified vendor
        - Orders by display_order for consistent dropdown ordering
        - Delegates response transformation to factory

        Args:
            vendor: Infrastructure vendor ('aws', 'azure', 'gcp', 'on_prem')

        Returns:
            Dict with vendor, supports_custom, regions list, and total count

        Example:
            >>> result = await service.get_regions_by_vendor('aws')
        """
        # Get regions from repository
        regions = await self.region_repository.get_regions_by_vendor(
            vendor=vendor,
            active_only=True
        )

        # Use factory to transform models to response format
        return make_regions_list_response(vendor, regions)
