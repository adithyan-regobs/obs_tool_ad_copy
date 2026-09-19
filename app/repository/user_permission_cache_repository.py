"""
User Permission Cache Repository

Handles database operations for user_permission_cache table.
Used to read/write cached permissions for fast lookups.
"""

from typing import Optional, List, Dict, Any
from sqlalchemy import select, and_, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.user_permission_cache_model import UserPermissionCacheModel
from app.core.enum import EnvironmentEnum


class UserPermissionCacheRepository:
    """Repository for user_permission_cache table operations."""

    def __init__(self, session: AsyncSession):
        """Initialize with database session."""
        self.session = session
        self.model = UserPermissionCacheModel

    async def get_cache(
        self,
        user_mst_code: str,
        tenants_mst_code: str,
        environment: Optional[EnvironmentEnum] = None
    ) -> Optional[UserPermissionCacheModel]:
        """
        Get cached permissions for a user.

        Args:
            user_mst_code: User's code
            tenants_mst_code: Tenant code
            environment: Environment (dev/staging/prod or None)

        Returns:
            Cache row with permissions JSONB, or None if not found
        """
        filters = [
            self.model.user_mst_code == user_mst_code,
            self.model.tenants_mst_code == tenants_mst_code,
            self.model.environment == environment,
        ]

        stmt = select(self.model).where(and_(*filters))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_cache(
        self,
        user_mst_code: str,
        tenants_mst_code: str,
        environment: Optional[EnvironmentEnum],
        permissions: List[Dict[str, Any]]
    ) -> UserPermissionCacheModel:
        """
        Insert or Update cache for a user.

        Args:
            user_mst_code: User's code
            tenants_mst_code: Tenant code
            environment: Environment (dev/staging/prod or None)
            permissions: List of {service_mst_code, policy_ref_code}

        Returns:
            Created or updated cache row
        """
        # Check if cache exists
        existing = await self.get_cache(user_mst_code, tenants_mst_code, environment)

        if existing:
            # UPDATE existing cache
            existing.permissions = permissions
            existing.updated_at = func.now()
            self.session.add(existing)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        else:
            # INSERT new cache
            new_cache = self.model(
                user_mst_code=user_mst_code,
                tenants_mst_code=tenants_mst_code,
                environment=environment,
                permissions=permissions
            )
            self.session.add(new_cache)
            await self.session.flush()
            await self.session.refresh(new_cache)
            return new_cache

    async def get_all_caches_for_user(
        self,
        user_mst_code: str,
        tenants_mst_code: str
    ) -> List[UserPermissionCacheModel]:
        """
        Get ALL cache rows for a user (across all environments).

        Used to get complete list of services user has access to.

        Args:
            user_mst_code: User's code
            tenants_mst_code: Tenant code

        Returns:
            List of all cache rows for this user
        """
        stmt = select(self.model).where(
            and_(
                self.model.user_mst_code == user_mst_code,
                self.model.tenants_mst_code == tenants_mst_code,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
