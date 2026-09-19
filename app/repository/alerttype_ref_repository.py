from typing import Optional, List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.alerttype_ref_model import AlertTypeRefModel
from app.repository.base_repository import BaseRepository


class AlertTypeRefRepository(BaseRepository[AlertTypeRefModel]):
    """Repository for Alert Type Reference operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(AlertTypeRefModel, session)

    async def get_by_code(self, code: str) -> Optional[AlertTypeRefModel]:
        """
        Get alert type by code.

        Args:
            code: Alert type code (e.g., 'cpu_util', 'http_4xx_rate', 'memory_util')

        Returns:
            AlertTypeRefModel or None if not found

        Example:
            >>> alert_type = await repo.get_by_code('cpu_util')
        """
        stmt = select(self.model).where(self.model.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_alert_types(self) -> List[AlertTypeRefModel]:
        """
        Get all non-deleted alert types.

        Returns:
            List of AlertTypeRefModel instances

        Example:
            >>> all_types = await repo.get_all_alert_types()
        """
        stmt = select(self.model).where(
            self.model.is_deleted == False
        ).order_by(self.model.name)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_active_alert_types(self) -> List[AlertTypeRefModel]:
        """
        Get all active (is_active=True) and non-deleted alert types.

        Returns:
            List of AlertTypeRefModel instances that are active

        Example:
            >>> active_types = await repo.get_active_alert_types()
        """
        stmt = select(self.model).where(
            self.model.is_active == True,
            self.model.is_deleted == False
        ).order_by(self.model.name)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def search_by_name(self, search_term: str) -> List[AlertTypeRefModel]:
        """
        Search alert types by name (case-insensitive partial match).

        Args:
            search_term: Search term to match against alert type names

        Returns:
            List of AlertTypeRefModel instances matching the search term

        Example:
            >>> cpu_alerts = await repo.search_by_name('cpu')
        """
        stmt = select(self.model).where(
            self.model.name.ilike(f"%{search_term}%"),
            self.model.is_deleted == False
        ).order_by(self.model.name)
        result = await self.session.execute(stmt)
        return result.scalars().all()