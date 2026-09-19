"""
Repository for GitHub App Installation Master table.
"""

from typing import Optional, List
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.base_repository import BaseRepository
from app.db.models.github_app_installation_mst_model import GitHubAppInstallationMstModel


class GitHubAppInstallationMstRepository(BaseRepository[GitHubAppInstallationMstModel]):
    def __init__(self, session: AsyncSession):
        super().__init__(GitHubAppInstallationMstModel, session)

    async def get_by_tenant_code(self, tenant_code: str) -> List[GitHubAppInstallationMstModel]:
        """Get all active installations for a tenant."""
        stmt = (
            select(self.model)
            .where(self.model.tenant_code == tenant_code)
            .where(self.model.is_active == True)
            .where(self.model.is_deleted == False)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_github_org(self, github_org: str) -> Optional[GitHubAppInstallationMstModel]:
        """Get active installation for a GitHub org."""
        stmt = (
            select(self.model)
            .where(self.model.github_org == github_org)
            .where(self.model.is_active == True)
            .where(self.model.is_deleted == False)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_installation(
        self,
        github_org: str,
        installation_id: str,
        tenant_code: str = "pending",
    ) -> GitHubAppInstallationMstModel:
        """Create or update an installation record using INSERT ... ON CONFLICT (race-safe)."""
        values = dict(
            code=f"ghai_{installation_id}",
            name=f"GitHub App Installation - {github_org}",
            description=f"GitHub App installed on org {github_org}",
            tenant_code=tenant_code,
            github_org=github_org,
            installation_id=installation_id,
            is_active=True,
            is_deleted=False,
        )

        update_set = dict(
            installation_id=installation_id,
            is_active=True,
            is_deleted=False,
        )
        if tenant_code != "pending":
            update_set["tenant_code"] = tenant_code

        stmt = (
            pg_insert(self.model)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["github_org"],
                set_=update_set,
            )
            .returning(self.model)
        )
        result = await self.session.execute(stmt)
        record = result.scalar_one()
        return record

    async def link_tenant(
        self,
        installation_id: str,
        tenant_code: str,
    ) -> Optional[GitHubAppInstallationMstModel]:
        """Link a pending installation to a tenant."""
        stmt = (
            select(self.model)
            .where(self.model.installation_id == installation_id)
            .where(self.model.is_deleted == False)
        )
        result = await self.session.execute(stmt)
        record = result.scalar_one_or_none()
        if not record:
            return None

        record.tenant_code = tenant_code
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return record

    async def deactivate_installation(
        self,
        installation_id: str,
    ) -> Optional[GitHubAppInstallationMstModel]:
        """Deactivate an installation (soft delete)."""
        stmt = (
            select(self.model)
            .where(self.model.installation_id == installation_id)
            .where(self.model.is_deleted == False)
        )
        result = await self.session.execute(stmt)
        record = result.scalar_one_or_none()
        if not record:
            return None

        record.is_active = False
        record.is_deleted = True
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return record
