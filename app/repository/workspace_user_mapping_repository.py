from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enum import WorkspaceRoleEnum
from app.db.models.user_mst_model import UserMstModel
from app.db.models.workspace_user_map_model import WorkspaceUserMapModel
from app.repository.base_repository import BaseRepository


class WorkspaceUserMappingRepository(BaseRepository[WorkspaceUserMapModel]):
    """Repository for workspace_user_mapping operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(WorkspaceUserMapModel, session)

    async def get_mapping(
        self,
        workspace_code: str,
        user_mst_code: str,
    ) -> Optional[WorkspaceUserMapModel]:
        stmt = select(self.model).where(
            self.model.workspace_code == workspace_code,
            self.model.user_mst_code == user_mst_code,
            self.model.is_deleted == False,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def caller_role_in_workspace(
        self,
        workspace_code: str,
        user_mst_code: str,
    ) -> Optional[WorkspaceRoleEnum]:
        """Return the caller's role in this workspace, or None if not mapped."""
        mapping = await self.get_mapping(workspace_code, user_mst_code)
        if mapping is None or mapping.is_active is False:
            return None
        return mapping.role

    async def list_members(
        self,
        workspace_code: str,
    ) -> List[Dict[str, Any]]:
        """
        Return active member rows for a workspace, joined with user_mst
        to expose human-readable identity (name + email).
        """
        stmt = (
            select(
                WorkspaceUserMapModel.id,
                WorkspaceUserMapModel.code.label("mapping_code"),
                WorkspaceUserMapModel.role,
                WorkspaceUserMapModel.created_at,
                WorkspaceUserMapModel.is_active,
                UserMstModel.code.label("user_code"),
                UserMstModel.first_name,
                UserMstModel.last_name,
                UserMstModel.email_id,
                UserMstModel.is_org_owner,
            )
            .select_from(WorkspaceUserMapModel)
            .join(
                UserMstModel,
                WorkspaceUserMapModel.user_mst_code == UserMstModel.code,
            )
            .where(WorkspaceUserMapModel.workspace_code == workspace_code)
            .where(WorkspaceUserMapModel.is_deleted == False)
            .where(UserMstModel.is_deleted == False)
            .order_by(WorkspaceUserMapModel.created_at.asc())
        )
        result = await self.session.execute(stmt)
        rows = result.all()
        return [
            {
                "id": row.id,
                "mapping_code": row.mapping_code,
                "user_code": row.user_code,
                "first_name": row.first_name,
                "last_name": row.last_name,
                "email_id": row.email_id,
                "role": row.role,
                "is_org_owner": row.is_org_owner,
                "is_active": row.is_active,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    async def soft_delete_mapping(
        self,
        workspace_code: str,
        user_mst_code: str,
    ) -> Optional[WorkspaceUserMapModel]:
        """
        Soft-remove a member: set is_deleted=True and is_active=False on
        the active mapping. Returns the updated row, or None if no active
        mapping exists.
        """
        mapping = await self.get_mapping(
            workspace_code=workspace_code,
            user_mst_code=user_mst_code,
        )
        if mapping is None:
            return None
        mapping.is_deleted = True
        mapping.is_active = False
        await self.session.flush()
        return mapping

    async def bulk_create(
        self,
        mappings: List[Dict[str, Any]],
    ) -> List[WorkspaceUserMapModel]:
        """
        Create many mapping rows in a single flush.

        Caller is expected to have pre-filtered for duplicates and inactive
        users — this method does not validate, it just persists.
        """
        if not mappings:
            return []

        objs = [self.model(**data) for data in mappings]
        self.session.add_all(objs)
        await self.session.flush()
        for obj in objs:
            await self.session.refresh(obj)
        return objs
