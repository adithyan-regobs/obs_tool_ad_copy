from typing import List
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.service_dependency_map_model import ServiceDependencyMapModel
from app.repository.base_repository import BaseRepository


class ServiceDependencyMapRepository(BaseRepository[ServiceDependencyMapModel]):
    """
    Repository for ServiceDependencyMap operations.
    Handles queries related to service-infrastructure dependencies.
    """

    def __init__(self, session: AsyncSession):
        super().__init__(ServiceDependencyMapModel, session)

    async def get_by_service_with_infrastructure(
        self, services_code: str
    ) -> List[ServiceDependencyMapModel]:
        """
        Get all dependency records for a service, with infrastructure details eagerly loaded.

        Args:
            services_code: The service code to fetch dependencies for

        Returns:
            List of ServiceDependencyMap records with infrastructure relationship loaded

        Example:
            service_deps = await repo.get_by_service_with_infrastructure("service_001")
            for dep in service_deps:
                print(f"Service depends on: {dep.infrastructure.infrastructuretype_ref_code}")
        """
        stmt = (
            select(self.model)
            .where(self.model.services_mst_code == services_code)
            .options(selectinload(self.model.infrastructure))
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()
