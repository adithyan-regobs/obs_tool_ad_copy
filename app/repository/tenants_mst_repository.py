"""
Repository for tenants_mst table operations
"""
from typing import Optional
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.base_repository import BaseRepository
from app.db.models.tenants_mst_model import TenantsMstModel


class TenantsMstRepository(BaseRepository[TenantsMstModel]):
    """Repository for tenants_mst table"""

    def __init__(self, session: AsyncSession):
        super().__init__(TenantsMstModel, session)

    async def get_by_code(self, code: str) -> Optional[TenantsMstModel]:
        """Get tenant by code"""
        stmt = select(TenantsMstModel).where(TenantsMstModel.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_subdomain(self, subdomain: str) -> Optional[TenantsMstModel]:
        """Get tenant by subdomain (only active, non-deleted tenants)"""
        stmt = select(TenantsMstModel).where(
            TenantsMstModel.subdomain == subdomain,
            TenantsMstModel.is_deleted == False,
            TenantsMstModel.is_active == True
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Optional[TenantsMstModel]:
        """Get tenant by name"""
        stmt = select(TenantsMstModel).where(
            TenantsMstModel.name == name,
            TenantsMstModel.is_deleted == False
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_tenant(
        self,
        name: str,
        subdomain: str,
        description: Optional[str] = None
    ) -> TenantsMstModel:
        """
        Create new tenant/organization with subdomain as code

        The code field is set to match the subdomain for easy JWT-based lookups.
        This allows us to find tenants by subdomain directly from JWT claims.
        """
        # Use subdomain as the tenant code for JWT authentication
        tenant_code = subdomain

        return await self.create(
            code=tenant_code,
            name=name,
            subdomain=subdomain,
            description=description or f"Organization for {name}",
            is_active=True,
            is_deleted=False
        )
