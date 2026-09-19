"""
Sidecar Configuration Repository
Handles data access operations for sidecar_configs table
"""
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.models.sidecar_config_model import SidecarConfigModel
from app.repository.base_repository import BaseRepository


class SidecarConfigRepository(BaseRepository[SidecarConfigModel]):
    """Repository for SidecarConfig operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(SidecarConfigModel, session)

    async def get_available_sidecars(
        self,
        app_code: str,
        rg_code: str,
        environment: str
    ) -> List[SidecarConfigModel]:
        """
        Get available sidecar configurations filtered by app, RG, and environment.

        Args:
            app_code: Application code
            rg_code: Resource group code
            environment: Environment (dev/staging/prod)

        Returns:
            List of available sidecar configurations
        """
        filters = [
            self.model.applications_mst_code == app_code,
            self.model.resource_group_mst_code == rg_code,
            self.model.environment == environment,
            self.model.is_deleted == False,
            self.model.is_active == True
        ]
        result = await self.session.execute(
            select(self.model).where(*filters)
        )
        return list(result.scalars().all())

    async def exists_by_code(self, code: str) -> bool:
        """
        Check if sidecar configuration exists by code.

        Args:
            code: Sidecar configuration code

        Returns:
            True if exists, False otherwise
        """
        config = await self.get_by(code=code, is_deleted=False)
        return config is not None

    async def get_by_name_and_env(
        self,
        app_code: str,
        rg_code: str,
        environment: str,
        name: str
    ) -> Optional[SidecarConfigModel]:
        """
        Get sidecar configuration by name for a specific app, RG, and environment.
        Used for mapping sidecars when cloning configs between environments.

        Args:
            app_code: Application code
            rg_code: Resource group code
            environment: Environment (dev/staging/prod)
            name: Sidecar name (e.g., "Datadog Agent")

        Returns:
            SidecarConfigModel if found, None otherwise
        """
        filters = [
            self.model.applications_mst_code == app_code,
            self.model.resource_group_mst_code == rg_code,
            self.model.environment == environment,
            self.model.name == name,
            self.model.is_deleted == False,
            self.model.is_active == True
        ]
        result = await self.session.execute(
            select(self.model).where(*filters)
        )
        return result.scalar_one_or_none()
