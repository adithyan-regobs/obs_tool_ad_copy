from typing import List, Optional
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.db_permission_mst_model import DbPermissionMstModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.repository.base_repository import BaseRepository


class DbPermissionMstRepository(BaseRepository[DbPermissionMstModel]):

    def __init__(self, session: AsyncSession):
        super().__init__(DbPermissionMstModel, session)

    async def get_by_server(self, infrastructure_mst_code: str) -> List[DbPermissionMstModel]:
        """Get all permissions for a given server (all levels), latest updated first."""
        stmt = (
            select(self.model)
            .where(
                self.model.infrastructure_mst_code == infrastructure_mst_code,
                self.model.is_deleted == False,
            )
            .order_by(self.model.updated_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_server_level(self, infrastructure_mst_code: str) -> List[DbPermissionMstModel]:
        """Get server-level permissions only (db_object_mst_id is null)."""
        stmt = (
            select(self.model)
            .where(
                self.model.infrastructure_mst_code == infrastructure_mst_code,
                self.model.db_object_mst_id == None,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_object(self, db_object_mst_id: int) -> List[DbPermissionMstModel]:
        """Get all permissions scoped to a specific database object."""
        stmt = (
            select(self.model)
            .where(
                self.model.db_object_mst_id == db_object_mst_id,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_username(
        self, infrastructure_mst_code: str, username: str
    ) -> List[DbPermissionMstModel]:
        """Get all permissions for a specific user on a server."""
        stmt = (
            select(self.model)
            .where(
                self.model.infrastructure_mst_code == infrastructure_mst_code,
                self.model.username == username,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_object_and_user(
        self, db_object_mst_id: Optional[int], username: str, infrastructure_mst_code: str
    ) -> Optional[DbPermissionMstModel]:
        """Find an existing permission entry for a specific object + user combination."""
        stmt = select(self.model).where(
            self.model.infrastructure_mst_code == infrastructure_mst_code,
            self.model.username == username,
            self.model.is_deleted == False,
        )
        if db_object_mst_id is not None:
            stmt = stmt.where(self.model.db_object_mst_id == db_object_mst_id)
        else:
            stmt = stmt.where(self.model.db_object_mst_id == None)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_server_username_filter(
        self,
        infrastructure_mst_code: str,
        username_pattern: str,
    ) -> List[DbPermissionMstModel]:
        """Get all permissions for a server filtered by a case-insensitive regex on username."""
        stmt = (
            select(self.model)
            .where(
                self.model.infrastructure_mst_code == infrastructure_mst_code,
                self.model.username.op("~*")(username_pattern),
                self.model.is_deleted == False,
            )
            .order_by(self.model.updated_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_usernames_across_servers(
        self,
        tenant_code: str,
        applications_mst_code: str,
        exclude_infrastructure_mst_code: str,
        username_prefix: str = "",
        environment: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> List[dict]:
        """
        Search distinct usernames (with password and server name) from sibling servers
        under the same application, excluding the current server.

        Joins db_permission_mst with infrastructure_mst to filter by application,
        and optionally by environment and geo location.

        Returns list of dicts: { username, password, server_name, infrastructure_mst_code, db_type }
        """
        infra = InfrastructureMstModel

        # Subquery: distinct (username, infrastructure_mst_code) with password from metadata_json
        stmt = (
            select(
                self.model.username,
                func.max(self.model.metadata_json["password"].astext).label("password"),
                self.model.infrastructure_mst_code,
                infra.name.label("server_name"),
                infra.infrastructuretype_ref_code,
            )
            .join(infra, self.model.infrastructure_mst_code == infra.code)
            .where(
                self.model.tenant_code == tenant_code,
                self.model.is_deleted == False,
                infra.is_deleted == False,
                infra.is_active == True,
                infra.applications_mst_code == applications_mst_code,
                self.model.infrastructure_mst_code != exclude_infrastructure_mst_code,
            )
            .group_by(
                self.model.username,
                self.model.infrastructure_mst_code,
                infra.name,
                infra.infrastructuretype_ref_code,
            )
            .order_by(self.model.username)
        )

        if username_prefix:
            stmt = stmt.where(self.model.username.ilike(f"{username_prefix}%"))

        if environment:
            stmt = stmt.where(infra.environments_enum == environment)

        if geo_loc_mst_code:
            stmt = stmt.where(infra.geo_loc_mst_code == geo_loc_mst_code)

        result = await self.session.execute(stmt)
        rows = result.all()

        return [
            {
                "username": row.username,
                "password": row.password,
                "server_name": row.server_name,
                "infrastructure_mst_code": row.infrastructure_mst_code,
                "infrastructuretype_ref_code": row.infrastructuretype_ref_code,
            }
            for row in rows
        ]
