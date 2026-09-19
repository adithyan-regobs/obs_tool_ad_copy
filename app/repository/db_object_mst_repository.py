from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.db_object_mst_model import DbObjectMstModel
from app.repository.base_repository import BaseRepository


class DbObjectMstRepository(BaseRepository[DbObjectMstModel]):

    def __init__(self, session: AsyncSession):
        super().__init__(DbObjectMstModel, session)

    async def get_by_server(self, infrastructure_mst_code: str) -> List[DbObjectMstModel]:
        """Get all objects for a given server."""
        stmt = (
            select(self.model)
            .where(
                self.model.infrastructure_mst_code == infrastructure_mst_code,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_server_and_type(
        self, infrastructure_mst_code: str, object_type: str
    ) -> List[DbObjectMstModel]:
        """Get all objects of a specific type for a server (e.g. all databases)."""
        stmt = (
            select(self.model)
            .where(
                self.model.infrastructure_mst_code == infrastructure_mst_code,
                self.model.type == object_type,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_children(self, parent_id: int) -> List[DbObjectMstModel]:
        """Get direct children of a parent object (e.g. schemas inside a database)."""
        stmt = (
            select(self.model)
            .where(
                self.model.parent_id == parent_id,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_server_name_type(
        self,
        infrastructure_mst_code: str,
        name: str,
        object_type: str,
        parent_id: Optional[int] = None,
    ) -> Optional[DbObjectMstModel]:
        """Find a specific object by server + name + type (+ optional parent)."""
        stmt = select(self.model).where(
            self.model.infrastructure_mst_code == infrastructure_mst_code,
            self.model.name == name,
            self.model.type == object_type,
            self.model.is_deleted == False,
        )
        if parent_id is not None:
            stmt = stmt.where(self.model.parent_id == parent_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
