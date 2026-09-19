from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.resource_connection_model import ResourceConnectionMstModel
from app.repository.base_repository import BaseRepository


class ResourceConnectionRepository(BaseRepository[ResourceConnectionMstModel]):

    def __init__(self, session: AsyncSession):
        super().__init__(ResourceConnectionMstModel, session)

    async def get_by_edge(
        self,
        source_table_name,
        source_transaction_code: str,
        target_table_name,
        target_transaction_code: str,
        environment,
    ) -> Optional[ResourceConnectionMstModel]:
        """Find the edge row for a directed (source → target, environment) pair.

        Deliberately does NOT filter is_deleted: the unique constraint spans
        soft-deleted rows too, so callers must revive an existing deleted edge
        instead of inserting a duplicate."""
        stmt = (
            select(self.model)
            .where(
                self.model.source_table_name == source_table_name,
                self.model.source_transaction_code == source_transaction_code,
                self.model.target_table_name == target_table_name,
                self.model.target_transaction_code == target_transaction_code,
                self.model.environments_enum == environment,
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_tenant_and_environment(
        self,
        tenant_code: str,
        environment,
    ) -> List[ResourceConnectionMstModel]:
        """All active edges for a tenant + environment (canvas edge fetch)."""
        stmt = select(self.model).where(
            self.model.tenants_mst_code == tenant_code,
            self.model.environments_enum == environment,
            self.model.is_deleted == False,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
