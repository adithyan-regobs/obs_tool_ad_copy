"""
Permission Service

Main service for checking user permissions.
Uses cache for fast lookups.
"""

from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.permission_cache_service import PermissionCacheService
from app.core.enum import EnvironmentEnum


class PermissionService:
    """Service for permission checking."""

    def __init__(self, session: AsyncSession):
        """
        Initialize with database session.

        Creates cache service instance.
        """
        self.session = session
        self.cache_service = PermissionCacheService(session)

    async def has_permission(
        self,
        user_mst_code: str,
        tenants_mst_code: str,
        service_mst_code: str,
        policy_ref_code: str,
        environment: Optional[EnvironmentEnum] = None
    ) -> bool:
        """
        Check if user has specific permission on a service.

        Args:
            user_mst_code: User's code
            tenants_mst_code: Tenant code
            service_mst_code: Service to check permission for
            policy_ref_code: Policy code (service_admin, service_manager, etc.)
            environment: Optional environment filter

        Returns:
            True if user has permission, False otherwise
        """
        # Step 1: Get permissions from cache
        permissions = await self.cache_service.get_cached_permissions(
            user_mst_code=user_mst_code,
            tenants_mst_code=tenants_mst_code,
            environment=environment
        )

        # Step 2: If no cache, return False (cache should be built first)
        if permissions is None:
            return False

        # Step 3: Check if permission exists in cache
        for perm in permissions:
            if (perm.get("service_mst_code") == service_mst_code and
                perm.get("policy_ref_code") == policy_ref_code):
                return True

        return False

    async def has_any_service_permission(
        self,
        user_mst_code: str,
        tenants_mst_code: str,
        service_mst_code: str
    ) -> bool:
        """
        Check if user has ANY permission on a service (any policy, any environment).
        Optimized: Checks cache directly without loading all services.

        Args:
            user_mst_code: User's code
            tenants_mst_code: Tenant code
            service_mst_code: Service to check

        Returns:
            True if user has any permission, False otherwise
        """
        # Get all cache rows for user (all environments)
        all_caches = await self.cache_service.cache_repo.get_all_caches_for_user(
            user_mst_code=user_mst_code,
            tenants_mst_code=tenants_mst_code
        )

        # Check if service exists in any cache
        for cache in all_caches:
            if cache.permissions:
                for perm in cache.permissions:
                    if perm.get("service_mst_code") == service_mst_code:
                        return True

        return False

