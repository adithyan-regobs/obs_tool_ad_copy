from typing import Any, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.workspace_mst_model import WorkspaceMstModel
from app.db.models.workspace_user_map_model import WorkspaceUserMapModel
from app.repository.base_repository import BaseRepository


class WorkspaceMstRepository(BaseRepository[WorkspaceMstModel]):
    """Repository for Workspace Master operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(WorkspaceMstModel, session)

    async def get_by_code(self, code: str) -> Optional[WorkspaceMstModel]:
        stmt = select(self.model).where(
            self.model.code == code,
            self.model.is_deleted == False,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_code_and_tenant(
        self,
        code: str,
        tenant_code: str,
    ) -> Optional[WorkspaceMstModel]:
        """
        Tenant-isolated lookup. Filters both `code` and `tenants_mst_code`
        in a single query so cross-tenant existence cannot be probed.
        """
        stmt = select(self.model).where(
            self.model.code == code,
            self.model.tenants_mst_code == tenant_code,
            self.model.is_deleted == False,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_name_and_tenant(
        self,
        name: str,
        tenant_code: str,
    ) -> Optional[WorkspaceMstModel]:
        """Service-layer uniqueness check for (tenant, name)."""
        stmt = select(self.model).where(
            self.model.name == name,
            self.model.tenants_mst_code == tenant_code,
            self.model.is_deleted == False,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_user_workspaces(
        self,
        user_code: str,
        tenant_code: str,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """
        Step 1 — query workspace_user_mapping by user + tenant to get workspace codes + roles.
        Step 2 — join workspace_mst to get name, description, status, is_active, timestamps.
        Step 3 — correlated subquery counts active, non-deleted members per workspace.
        """
        member_count_subq = (
            select(func.count(WorkspaceUserMapModel.id))
            .where(
                WorkspaceUserMapModel.workspace_code == WorkspaceMstModel.code,
                WorkspaceUserMapModel.is_active == True,
                WorkspaceUserMapModel.is_deleted == False,
            )
            .correlate(WorkspaceMstModel)
            .scalar_subquery()
        )

        stmt = (
            select(
                WorkspaceUserMapModel.workspace_code,
                WorkspaceUserMapModel.role.label("user_role"),
                WorkspaceMstModel.id,
                WorkspaceMstModel.name,
                WorkspaceMstModel.description,
                WorkspaceMstModel.status,
                WorkspaceMstModel.is_active,
                WorkspaceMstModel.tenants_mst_code,
                WorkspaceMstModel.created_at,
                WorkspaceMstModel.updated_at,
                member_count_subq.label("member_count"),
            )
            .select_from(WorkspaceUserMapModel)
            .join(
                WorkspaceMstModel,
                WorkspaceUserMapModel.workspace_code == WorkspaceMstModel.code,
            )
            .where(
                WorkspaceUserMapModel.user_mst_code == user_code,
                WorkspaceUserMapModel.tenants_mst_code == tenant_code,
                WorkspaceUserMapModel.is_active == True,
                WorkspaceUserMapModel.is_deleted == False,
                WorkspaceMstModel.is_deleted == False,
            )
        )

        if is_active is not None:
            stmt = stmt.where(WorkspaceMstModel.is_active == is_active)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar() or 0

        stmt = stmt.order_by(WorkspaceMstModel.created_at.asc()).offset(skip).limit(limit)
        rows = (await self.session.execute(stmt)).all()

        return {
            "total": total,
            "workspaces": [
                {
                    "id": row.id,
                    "code": row.workspace_code,
                    "name": row.name,
                    "description": row.description,
                    "tenant_code": row.tenants_mst_code,
                    "status": row.status.value if row.status else "active",
                    "is_active": row.is_active,
                    "user_role": row.user_role.value if row.user_role else None,
                    "member_count": row.member_count or 0,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
                for row in rows
            ],
        }

