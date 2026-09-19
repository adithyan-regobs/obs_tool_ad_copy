"""
Repository for role_mst table operations
"""
from typing import Optional
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.base_repository import BaseRepository
from app.db.models.user_mst_model import RoleMst


class RoleMstRepository(BaseRepository[RoleMst]):
    """Repository for role_mst table"""

    def __init__(self, session: AsyncSession):
        super().__init__(RoleMst, session)

    async def get_by_user_id(self, user_mst_id: int) -> Optional[RoleMst]:
        """Get role by user_mst_id"""
        stmt = select(RoleMst).where(
            RoleMst.user_mst_id == user_mst_id,
            RoleMst.is_deleted == False,
            RoleMst.is_active == True
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_role(
        self,
        user_mst_id: int,
        role_name: str,
        description: str = None
    ) -> RoleMst:
        """
        Create new role for a user

        Args:
            user_mst_id: The user's ID
            role_name: Role name (e.g., 'Admin', 'User', 'Viewer')
            description: Optional description for the role
        """
        # Generate unique role code using UUID
        role_code = str(uuid.uuid4())

        return await self.create(
            code=role_code,
            name=role_name,
            description=description or f"{role_name} role",
            user_mst_id=user_mst_id,
            is_active=True,
            is_deleted=False
        )

    async def update_role(
        self,
        role: RoleMst,
        role_name: Optional[str] = None,
        description: Optional[str] = None
    ) -> RoleMst:
        """Update existing role"""
        updates = {}

        if role_name:
            updates['name'] = role_name
        if description:
            updates['description'] = description

        return await self.update(role, updates)

    async def delete_by_user_id(self, user_mst_id: int) -> bool:
        """Soft delete all roles for a user"""
        stmt = select(RoleMst).where(RoleMst.user_mst_id == user_mst_id)
        result = await self.session.execute(stmt)
        roles = result.scalars().all()

        for role in roles:
            role.is_deleted = True
            role.is_active = False

        return True