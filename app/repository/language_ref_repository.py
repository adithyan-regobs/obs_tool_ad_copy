"""
Language Reference Repository
Handles data access operations for language_ref table
"""
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.models.language_ref_model import LanguageRefModel
from app.repository.base_repository import BaseRepository


def _double_k8s_resource(value: str) -> str:
    """Double a Kubernetes resource quantity, e.g. '500m' → '1000m', '512Mi' → '1024Mi'."""
    for suffix in ("Mi", "Gi", "m", ""):
        if value.endswith(suffix):
            numeric = value[: len(value) - len(suffix)] if suffix else value
            try:
                return f"{int(numeric) * 2}{suffix}"
            except ValueError:
                break
    return value


class LanguageRefRepository(BaseRepository[LanguageRefModel]):
    """Repository for LanguageRef operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(LanguageRefModel, session)

    async def find_for_mcp(
        self,
        language_name: str,
        language_version: Optional[str] = None,
    ) -> Optional[LanguageRefModel]:
        """Resolve a human-readable language name (e.g. 'python') + optional version
        (e.g. '3.10') to a LanguageRefModel.

        Match strategy:
          1. If version is provided: name ilike + exact version match → first result.
          2. If version omitted (or no exact match): name ilike only → first result
             ordered by version desc (latest).

        Used by the DevLift MCP `provision_service` tool to convert the LLM-provided
        language string into the canonical language_ref_code.
        """
        name_filter = self.model.name.ilike(f"%{language_name.strip()}%")
        base_stmt = (
            select(self.model)
            .where(
                self.model.is_active == True,   # noqa: E712
                self.model.is_deleted == False,  # noqa: E712
                name_filter,
            )
        )

        if language_version:
            stmt = base_stmt.where(self.model.version == language_version.strip()).limit(1)
            result = await self.session.execute(stmt)
            row = result.scalar_one_or_none()
            if row:
                return row

        # Fallback: latest version for the language
        stmt = base_stmt.order_by(self.model.version.desc()).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Optional[LanguageRefModel]:
        """
        Get language reference by code.

        Args:
            code: Language reference code (e.g., 'PYTHON_3_12')

        Returns:
            Language reference if found, None otherwise
        """
        return await self.get_by(code=code)

    async def get_all_active(self) -> List[LanguageRefModel]:
        """
        Get all active language references.

        Returns:
            List of active language references ordered by name and version
        """
        filters = [self.model.is_active == True, self.model.is_deleted == False]
        result = await self.session.execute(
            select(self.model)
            .where(*filters)
            .order_by(self.model.name, self.model.version)
        )
        return list(result.scalars().all())

    async def get_by_platform(self, platform: str, is_active: bool = True) -> List[LanguageRefModel]:
        """
        Get all language references that support a specific CI/CD platform.

        Uses JSONB query to check if the platform key exists in yaml_templates.

        Args:
            platform: Platform name (e.g., 'github_actions', 'gitlab_ci')
            is_active: Filter by active status (default: True)

        Returns:
            List of language references that support the specified platform
        """
        filters = [
            self.model.is_deleted == False,
            self.model.yaml_templates.has_key(platform)  # JSONB ? operator
        ]

        if is_active is not None:
            filters.append(self.model.is_active == is_active)

        result = await self.session.execute(
            select(self.model)
            .where(*filters)
            .order_by(self.model.name, self.model.version)
        )
        return list(result.scalars().all())

    async def get_filtered(
        self,
        platform: Optional[str] = None,
        is_active: bool = True
    ) -> List[LanguageRefModel]:
        """
        Get language references with optional filters.

        Args:
            platform: Optional platform filter (checks JSONB for key existence)
            is_active: Filter by active status (default: True)

        Returns:
            List of filtered language references
        """
        filters = [
            self.model.is_deleted == False
        ]

        if is_active is not None:
            filters.append(self.model.is_active == is_active)

        if platform is not None:
            filters.append(self.model.yaml_templates.has_key(platform))

        result = await self.session.execute(
            select(self.model)
            .where(*filters)
            .order_by(self.model.name, self.model.version)
        )
        return list(result.scalars().all())
