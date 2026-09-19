"""
Repository for user_mst table operations
"""
from typing import List, Optional
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.base_repository import BaseRepository
from app.db.models.user_mst_model import UserMstModel
from app.db.models.workspace_user_map_model import WorkspaceUserMapModel
from app.core.enum import AuthProviderEnum


class UserMstRepository(BaseRepository[UserMstModel]):
    """Repository for user_mst table"""

    def __init__(self, session: AsyncSession):
        super().__init__(UserMstModel, session)

    async def get_by_code(self, code: str) -> Optional[UserMstModel]:
        """Get user by code (only active, non-deleted users)"""
        stmt = select(UserMstModel).where(
            UserMstModel.code == code,
            UserMstModel.is_deleted == False,
            UserMstModel.is_active == True
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_auth_provider_id(self, auth_provider_id: str) -> Optional[UserMstModel]:
        """Get user by auth provider ID (only active, non-deleted users)"""
        stmt = select(UserMstModel).where(
            UserMstModel.auth_provider_id == auth_provider_id,
            UserMstModel.is_deleted == False,
            UserMstModel.is_active == True
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Optional[UserMstModel]:
        """Get user by email"""
        stmt = select(UserMstModel).where(UserMstModel.email_id == email)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_user(
        self,
        auth_provider_id: str,
        email: str,
        first_name: str,
        last_name: str,
        tenant_code: str,
        is_org_owner: bool = False,
        auth_provider: AuthProviderEnum = AuthProviderEnum.clerk
    ) -> UserMstModel:
        """
        Create new user with auto-generated UUID code

        Industry standard approach: generates a unique UUID v4 for the user code
        """
        # Generate unique user code using UUID (industry standard)
        user_code = str(uuid.uuid4())

        # Create user name
        user_name = f"{first_name} {last_name}"

        return await self.create(
            code=user_code,
            name=user_name,
            auth_provider_id=auth_provider_id,
            auth_provider=auth_provider,
            email_id=email,
            first_name=first_name,
            last_name=last_name,
            tenants_mst_code=tenant_code,
            is_org_owner=is_org_owner,
            is_active=True,
            is_deleted=False
        )

    async def list_eligible_for_workspace(
        self,
        tenant_code: str,
        workspace_code: str,
    ) -> List[UserMstModel]:
        """
        Active, non-deleted users in `tenant_code` who do NOT already have
        an active mapping in `workspace_code`.

        Used by the Add-Users picker so the UI doesn't need to filter
        client-side.
        """
        existing_subquery = (
            select(WorkspaceUserMapModel.user_mst_code)
            .where(WorkspaceUserMapModel.workspace_code == workspace_code)
            .where(WorkspaceUserMapModel.is_deleted == False)
        )

        stmt = (
            select(UserMstModel)
            .where(UserMstModel.tenants_mst_code == tenant_code)
            .where(UserMstModel.is_deleted == False)
            .where(UserMstModel.is_active == True)
            .where(UserMstModel.code.notin_(existing_subquery))
            .order_by(UserMstModel.first_name.asc(), UserMstModel.last_name.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update_user(
        self,
        user: UserMstModel,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        tenant_code: Optional[str] = None,
        is_org_owner: Optional[bool] = None
    ) -> UserMstModel:
        """Update existing user"""
        updates = {}

        if first_name:
            updates['first_name'] = first_name
        if last_name:
            updates['last_name'] = last_name
        if first_name or last_name:
            updates['name'] = f"{first_name or user.first_name} {last_name or user.last_name}"
        if email:
            updates['email_id'] = email
        if tenant_code:
            updates['tenants_mst_code'] = tenant_code
        if is_org_owner is not None:
            updates['is_org_owner'] = is_org_owner

        return await self.update(user, updates)
