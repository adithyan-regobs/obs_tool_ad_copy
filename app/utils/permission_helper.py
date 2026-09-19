"""
Permission Helper Service

Centralized permission checking with org_owner bypass.
Provides capability-based access control (can_read, can_write, can_manage).
"""

from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.permission_cache_service import PermissionCacheService
from app.core.enum import EnvironmentEnum
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo


class PermissionHelper:
    """
    Centralized permission checking with org_owner bypass.

    Usage:
        helper = PermissionHelper(db)
        if await helper.check_write_access(user, tenant, service_code, env):
            # Allow write operation
    """

    def __init__(self, session: AsyncSession):
        """Initialize with database session."""
        self.session = session
        self.cache_service = PermissionCacheService(session)

    async def is_org_owner(self, user: UserMstModel) -> bool:
        """
        Check if user is organization owner.

        Org owners bypass all permission checks - they have full access.

        Args:
            user: User model instance

        Returns:
            True if user is org_owner, False otherwise
        """
        return user.is_org_owner is True

    async def _check_capability(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel,
        service_code: str,
        environment: Optional[EnvironmentEnum],
        capability: str
    ) -> bool:
        """
        Internal method to check specific capability from cache.

        Args:
            user: User model
            tenant: Tenant model
            service_code: Service code to check
            environment: Environment filter (None = check all)
            capability: Capability to check (can_read, can_write, can_manage)

        Returns:
            True if user has the capability, False otherwise
        """
        permissions = await self.cache_service.get_cached_permissions(
            user_mst_code=user.code,
            tenants_mst_code=tenant.code,
            environment=environment
        )

        if not permissions:
            return False

        for perm in permissions:
            if perm.get("service_mst_code") == service_code:
                if perm.get(capability, False):
                    return True

        return False

    async def check_read_access(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel,
        service_code: str,
        environment: Optional[EnvironmentEnum] = None
    ) -> bool:
        """
        Check if user has READ access to a service.

        Args:
            user: User model instance
            tenant: Tenant model instance
            service_code: Service code to check
            environment: Optional environment filter

        Returns:
            True if user can read, False otherwise
        """
        if await self.is_org_owner(user):
            return True

        return await self._check_capability(
            user, tenant, service_code, environment, "can_read"
        )

    async def check_write_access(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel,
        service_code: str,
        environment: Optional[EnvironmentEnum] = None
    ) -> bool:
        """
        Check if user has WRITE access to a service (POST/PUT).

        Args:
            user: User model instance
            tenant: Tenant model instance
            service_code: Service code to check
            environment: Optional environment filter

        Returns:
            True if user can write, False otherwise
        """
        if await self.is_org_owner(user):
            return True

        return await self._check_capability(
            user, tenant, service_code, environment, "can_write"
        )

    async def check_manage_access(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel,
        service_code: str,
        environment: Optional[EnvironmentEnum] = None
    ) -> bool:
        """
        Check if user has MANAGE access (can grant/revoke permissions).

        Args:
            user: User model instance
            tenant: Tenant model instance
            service_code: Service code to check
            environment: Optional environment filter

        Returns:
            True if user can manage permissions, False otherwise
        """
        if await self.is_org_owner(user):
            return True

        return await self._check_capability(
            user, tenant, service_code, environment, "can_manage"
        )

    async def get_allowed_services_for_read(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel
    ) -> List[str]:
        """
        Get list of services user can READ.

        Args:
            user: User model
            tenant: Tenant model

        Returns:
            List of service codes, or ["*"] for org_owner (all services)
        """
        if await self.is_org_owner(user):
            return ["*"]

        return await self.cache_service.get_allowed_services(
            user_mst_code=user.code,
            tenants_mst_code=tenant.code
        )

    async def get_allowed_environments_for_write(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel,
        service_code: str
    ) -> List[str]:
        """
        Get environments user can WRITE to for a service.

        Args:
            user: User model
            tenant: Tenant model
            service_code: Service code

        Returns:
            List of environment values (dev, staging, qa, prod)
        """
        if await self.is_org_owner(user):
            return resource_meta_repo.get_all_environment_values()

        return await self.cache_service.get_allowed_environments(
            user_mst_code=user.code,
            tenants_mst_code=tenant.code,
            service_mst_code=service_code
        )
