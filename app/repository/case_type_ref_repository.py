from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.models.case_type_ref_model import CaseTypeRefModel
from app.repository.base_repository import BaseRepository


class CaseTypeRefRepository(BaseRepository[CaseTypeRefModel]):
    def __init__(self, session: AsyncSession):
        super().__init__(CaseTypeRefModel, session)

    async def get_all(self) -> List[CaseTypeRefModel]:
        stmt = (
            select(self.model)
            .where(
                 self.model.is_deleted == False,
                 self.model.is_active == True
             )
            .order_by(self.model.code)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_code(self, code: str) -> Optional[CaseTypeRefModel]:
        stmt = (
            select(self.model)
            .where(self.model.code == code)
            .where(self.model.is_deleted == False)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()