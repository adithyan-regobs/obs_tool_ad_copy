from typing import Optional, List

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enum import LogProviderEnum
from app.db.models.log_provider_config_model import LogProviderConfigModel
from app.repository.base_repository import BaseRepository


class LogProviderConfigRepository(BaseRepository[LogProviderConfigModel]):
    """Repository for log provider configuration operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(LogProviderConfigModel, session)

    async def get_by_tenant_and_provider(
        self, tenant_code: str, provider: LogProviderEnum
    ) -> Optional[LogProviderConfigModel]:
        """Get a specific provider config for a tenant."""
        stmt = select(self.model).where(
            and_(
                self.model.tenants_mst_code == tenant_code,
                self.model.provider == provider,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_default_for_tenant(
        self, tenant_code: str
    ) -> Optional[LogProviderConfigModel]:
        """Get the default log provider for a tenant."""
        stmt = select(self.model).where(
            and_(
                self.model.tenants_mst_code == tenant_code,
                self.model.is_default == True,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_for_tenant(
        self, tenant_code: str
    ) -> List[LogProviderConfigModel]:
        """List all log provider configs for a tenant."""
        stmt = (
            select(self.model)
            .where(
                and_(
                    self.model.tenants_mst_code == tenant_code,
                    self.model.is_deleted == False,
                )
            )
            .order_by(self.model.is_default.desc(), self.model.created_at)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
