from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.case_type_ref_repository import CaseTypeRefRepository
from app.repository.case_ref_repository import CaseRefRepository


class CaseReferenceService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.case_type_repo = CaseTypeRefRepository(session)
        self.case_ref_repo = CaseRefRepository(session)

    async def get_all_case_types(self):
        return await self.case_type_repo.get_all()

    async def get_case_refs(self, case_type_ref_code: Optional[str] = None):
        return await self.case_ref_repo.get_all(case_type_ref_code)

    async def search_case_refs(self, search_query: str) -> List:
        """
        Search case references by space-separated search terms.
        If search_query is empty or whitespace-only, returns all case refs.

        Args:
            search_query: Space-separated search terms

        Returns:
            List of matching case refs with joined case_type_ref data
        """
        search_terms = [t.strip() for t in search_query.split() if t.strip()]
        if not search_terms:
            return await self.case_ref_repo.get_all_with_case_type()
        return await self.case_ref_repo.search_with_case_type(search_terms)