from typing import List, Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from app.db.models.case_ref_model import CaseRefModel
from app.db.models.case_type_ref_model import CaseTypeRefModel
from app.repository.base_repository import BaseRepository


class CaseRefRepository(BaseRepository[CaseRefModel]):
    def __init__(self, session: AsyncSession):
        super().__init__(CaseRefModel, session)

    async def get_all(self, case_type_ref_code: Optional[str] = None) -> List[CaseRefModel]:
        filters = [self.model.is_deleted == False]

        if case_type_ref_code:
            filters.append(self.model.case_type_ref_code == case_type_ref_code)

        stmt = (
            select(self.model)
            .where(*filters)
            .order_by(self.model.code)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_code(self, code: str) -> Optional[CaseRefModel]:
        stmt = (
            select(self.model)
            .where(self.model.code == code)
            .where(self.model.is_deleted == False)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def search_with_case_type(self, search_terms: List[str]) -> List[Dict[str, Any]]:
        """
        Search case_ref joined with case_type_ref where any term matches
        case_ref.name OR case_type_ref.name (case-insensitive ILIKE).

        Args:
            search_terms: List of search terms to match

        Returns:
            List of dicts with case_ref and case_type_ref data
        """
        # Build OR conditions for each term against both case_ref.name and case_type_ref.name
        or_conditions = []
        for term in search_terms:
            pattern = f"%{term}%"
            or_conditions.append(CaseRefModel.name.ilike(pattern))
            or_conditions.append(CaseTypeRefModel.name.ilike(pattern))

        stmt = (
            select(
                CaseRefModel.id,
                CaseRefModel.code,
                CaseRefModel.name,
                CaseRefModel.case_type_ref_code,
                CaseTypeRefModel.name.label('case_type_name')
            )
            .outerjoin(
                CaseTypeRefModel,
                CaseRefModel.case_type_ref_code == CaseTypeRefModel.code
            )
            .where(
                CaseRefModel.is_deleted == False,
                CaseTypeRefModel.is_active == True,
                or_(*or_conditions)
            )
            .order_by(CaseRefModel.name)
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        return [
            {
                "id": row.id,
                "code": row.code,
                "name": row.name,
                "case_type_ref_code": row.case_type_ref_code,
                "case_type_name": row.case_type_name
            }
            for row in rows
        ]

    async def get_all_with_case_type(self) -> List[Dict[str, Any]]:
        """
        Get all case_ref records joined with case_type_ref.

        Returns:
            List of dicts with case_ref and case_type_ref data
        """
        stmt = (
            select(
                CaseRefModel.id,
                CaseRefModel.code,
                CaseRefModel.name,
                CaseRefModel.case_type_ref_code,
                CaseTypeRefModel.name.label('case_type_name')
            )
            .outerjoin(
                CaseTypeRefModel,
                CaseRefModel.case_type_ref_code == CaseTypeRefModel.code
            )
            .where(
                CaseRefModel.is_deleted == False,
                CaseTypeRefModel.is_active == True
            )
            .order_by(CaseRefModel.name)
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        return [
            {
                "id": row.id,
                "code": row.code,
                "name": row.name,
                "case_type_ref_code": row.case_type_ref_code,
                "case_type_name": row.case_type_name
            }
            for row in rows
        ]