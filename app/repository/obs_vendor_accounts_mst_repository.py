"""
User repository with specific user operations
"""

from typing import Optional, List
from uuid import UUID
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.enum import ObsVendorEnum
from app.core.config import settings

from app.db.models.obs_vendor_accounts_mst_model import ObsVendorAccountsMstModel
from app.repository.base_repository import BaseRepository


class ObsVendorAccountsMstRepository(BaseRepository[ObsVendorAccountsMstModel]):
    """Repository for Obs Vendor Accounts Master operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ObsVendorAccountsMstModel, session)

    async def get_by_service_hierarchy(
        self,
        service_code: Optional[str] = None,
        application_code: Optional[str] = None,
        tenant_code: Optional[str] = None
    ) -> Optional[ObsVendorAccountsMstModel]:
        """
        Get vendor account using hierarchical lookup with tenant isolation.

        Lookup priority with proper isolation:
        1. Service-specific (within application and tenant)
        2. Application-level (within tenant)
        3. Tenant-level (fallback)

        Multi-tenant isolation ensures:
        - Services are only searched within their parent application and tenant
        - Applications are only searched within their parent tenant
        - Each level maintains the hierarchy: Tenant → Application → Service

        Note: All vendor accounts MUST have a tenant. No global fallback exists.
        """
        # Tenant is required for multi-tenant isolation
        if not tenant_code:
            return None

        # Build query with tenant isolation and OR conditions for hierarchy
        stmt = select(self.model).where(
            self.model.tenants_mst_code == tenant_code,  # Always filter by tenant
            or_(
                # Level 1: Service-specific account (within application and tenant)
                (self.model.services_mst_code == service_code) &
                (self.model.applications_mst_code == application_code),
                # Level 2: Application-level account (within tenant)
                (self.model.services_mst_code.is_(None)) &
                (self.model.applications_mst_code == application_code),
                # Level 3: Tenant-level account (fallback)
                (self.model.services_mst_code.is_(None)) &
                (self.model.applications_mst_code.is_(None))
            )
        ).order_by(
            # Order by specificity: service > app > tenant
            self.model.services_mst_code.desc().nullslast(),
            self.model.applications_mst_code.desc().nullslast()
        ).limit(1)

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()