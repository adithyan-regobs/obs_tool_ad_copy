"""
Permission Cache Service

Business logic for building and rebuilding user permission cache.
Uses both repositories to read raw permissions and write to cache.
"""

import logging
from typing import List, Dict, Any, Optional
from collections import defaultdict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.repository.service_user_permission_repository import ServiceUserPermissionRepository
from app.repository.user_permission_cache_repository import UserPermissionCacheRepository
from app.db.models.policy_ref_model import PolicyRefModel
from app.db.models.user_mst_model import UserMstModel, RoleMst
from app.core.enum import EnvironmentEnum
from app.utils.service_lookup_helper import get_services_by_rg, get_services_by_app
from app.utils.role_lookup_helper import get_user_role_type_codes

logger = logging.getLogger(__name__)


class PermissionCacheService:
    """Service for permission cache operations."""

    def __init__(self, session: AsyncSession):
        """Initialize with database session."""
        self.session = session
        self.permission_repo = ServiceUserPermissionRepository(session)
        self.cache_repo = UserPermissionCacheRepository(session)

    async def rebuild_cache(
        self,
        user_mst_code: str,
        tenants_mst_code: str,
        role_type_codes: Optional[List[str]] = None,
        environment: Optional[EnvironmentEnum] = None
    ) -> Dict[str, Any]:
        """
        Rebuild permission cache for a user.

        Flow:
        1. If role_type_codes not provided, lookup user and get their roles
        2. Get user's direct permissions (by user_mst_code)
        3. Get role-based permissions (by role_type_codes)
        4. Merge both
        5. Group by environment (from permission rows)
        6. For each environment: expand and save cache row

        Args:
            user_mst_code: User's code
            tenants_mst_code: Tenant code
            role_type_codes: User's role type codes (optional - will lookup if not provided)
            environment: Not used for grouping - we get env FROM permissions

        Returns:
            Dict with cache info
        """
        # If role_type_codes not provided, lookup user and get their roles
        if role_type_codes is None:
            stmt = select(UserMstModel).where(UserMstModel.code == user_mst_code)
            result = await self.session.execute(stmt)
            user = result.scalar_one_or_none()

            if user:
                role_type_codes = await get_user_role_type_codes(self.session, user.id)
            else:
                role_type_codes = []
                logger.warning(f"[CACHE_SVC] User not found: {user_mst_code}")

        logger.info(f"[CACHE_SVC] rebuild_cache: user={user_mst_code}, tenant={tenants_mst_code}, roles={role_type_codes}")

        # Step 1: Get user's direct permissions (no env filter - get ALL)
        user_permissions = await self.permission_repo.get_by_user(
            user_mst_code=user_mst_code,
            tenants_mst_code=tenants_mst_code,
            environment=None  # Get ALL environments
        )
        logger.info(f"[CACHE_SVC] Found {len(user_permissions)} direct user permissions")

        # Step 2: Get role-based permissions (no env filter - get ALL)
        role_permissions = await self.permission_repo.get_by_role_types(
            role_type_codes=role_type_codes,
            tenants_mst_code=tenants_mst_code,
            environment=None  # Get ALL environments
        )
        logger.info(f"[CACHE_SVC] Found {len(role_permissions)} role-based permissions")

        # Step 3: Merge all permissions
        all_permissions = user_permissions + role_permissions
        logger.info(f"[CACHE_SVC] Total merged permissions: {len(all_permissions)}")

        if not all_permissions:
            logger.info(f"[CACHE_SVC] No permissions found")
            return {"user_mst_code": user_mst_code, "permissions_count": 0, "cache_ids": []}

        # Step 4: Group by environment (from permission row)
        env_groups: Dict[Optional[EnvironmentEnum], List] = defaultdict(list)
        for perm in all_permissions:
            env_groups[perm.environment].append(perm)

        env_names = [e.value if e else "NULL" for e in env_groups.keys()]
        logger.info(f"[CACHE_SVC] Grouped by environments: {env_names}")

        # Step 5: For each environment, expand and save cache
        cache_ids = []
        total_count = 0

        for env, perms in env_groups.items():
            expanded = await self._expand_permissions(perms, tenants_mst_code)
            logger.info(f"[CACHE_SVC] Env={env.value if env else 'NULL'}: {len(expanded)} expanded permissions")

            cache_row = await self.cache_repo.upsert_cache(
                user_mst_code=user_mst_code,
                tenants_mst_code=tenants_mst_code,
                environment=env,
                permissions=expanded
            )
            cache_ids.append(cache_row.id)
            total_count += len(expanded)
            logger.info(f"[CACHE_SVC] Cache saved: id={cache_row.id}, env={env.value if env else 'NULL'}")

        logger.info(f"[CACHE_SVC] Complete: {len(cache_ids)} cache rows, {total_count} total permissions")

        return {
            "user_mst_code": user_mst_code,
            "permissions_count": total_count,
            "cache_ids": cache_ids,
            "environments": env_names
        }

    async def _get_policy_capabilities(self) -> Dict[str, Dict[str, bool]]:
        """
        Fetch all policy capabilities from policy_ref table.

        Returns:
            Dict mapping policy_code to capabilities:
            {
                "service_admin": {"can_read": True, "can_write": True, "can_manage": True},
                "service_manager": {"can_read": True, "can_write": True, "can_manage": False},
                ...
            }
        """
        stmt = select(PolicyRefModel).where(
            PolicyRefModel.is_deleted == False,
            PolicyRefModel.is_active == True
        )
        result = await self.session.execute(stmt)
        policies = result.scalars().all()

        return {
            p.code: {
                "can_read": p.can_read,
                "can_write": p.can_write,
                "can_manage": p.can_manage
            }
            for p in policies
        }

    async def _expand_permissions(
        self,
        permissions: List,
        tenants_mst_code: str
    ) -> List[Dict[str, Any]]:
        """
        Expand permissions from RG/App level to service level.

        Args:
            permissions: List of permission rows (same environment)
            tenants_mst_code: Tenant code for service lookup

        Returns:
            List of {service_mst_code, policy_ref_code, can_read, can_write, can_manage}
        """
        result_set = set()
        result_list = []

        # Get policy capabilities lookup
        policy_caps = await self._get_policy_capabilities()

        for perm in permissions:
            policy = perm.policy_ref_code
            caps = policy_caps.get(policy, {"can_read": False, "can_write": False, "can_manage": False})

            # Case 1: Service level (most specific)
            if perm.services_mst_code:
                key = (perm.services_mst_code, policy)
                if key not in result_set:
                    result_set.add(key)
                    result_list.append({
                        "service_mst_code": perm.services_mst_code,
                        "policy_ref_code": policy,
                        "can_read": caps["can_read"],
                        "can_write": caps["can_write"],
                        "can_manage": caps["can_manage"]
                    })

            # Case 2: RG level - expand to services under RG
            elif perm.resource_group_mst_code:
                services = await get_services_by_rg(self.session, perm.resource_group_mst_code, tenants_mst_code)
                for svc in services:
                    key = (svc, policy)
                    if key not in result_set:
                        result_set.add(key)
                        result_list.append({
                            "service_mst_code": svc,
                            "policy_ref_code": policy,
                            "can_read": caps["can_read"],
                            "can_write": caps["can_write"],
                            "can_manage": caps["can_manage"]
                        })

            # Case 3: App level - expand to services under App
            elif perm.applications_mst_code:
                services = await get_services_by_app(self.session, perm.applications_mst_code, tenants_mst_code)
                for svc in services:
                    key = (svc, policy)
                    if key not in result_set:
                        result_set.add(key)
                        result_list.append({
                            "service_mst_code": svc,
                            "policy_ref_code": policy,
                            "can_read": caps["can_read"],
                            "can_write": caps["can_write"],
                            "can_manage": caps["can_manage"]
                        })

        return result_list

    async def get_cached_permissions(
        self,
        user_mst_code: str,
        tenants_mst_code: str,
        environment: Optional[EnvironmentEnum] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Get permissions from cache.

        Args:
            user_mst_code: User's code
            tenants_mst_code: Tenant code
            environment: Environment filter

        Returns:
            List of permissions if cache exists, None if not found
        """
        cache = await self.cache_repo.get_cache(
            user_mst_code=user_mst_code,
            tenants_mst_code=tenants_mst_code,
            environment=environment
        )

        if cache:
            return cache.permissions
        return None

    async def get_allowed_services(
        self,
        user_mst_code: str,
        tenants_mst_code: str
    ) -> List[str]:
        """
        Get list of all service codes user has permission for (any environment).

        Used to filter service list on the Services page.

        Args:
            user_mst_code: User's code
            tenants_mst_code: Tenant code

        Returns:
            List of unique service_mst_code values
        """
        # Get all cache rows for user (all environments)
        all_caches = await self.cache_repo.get_all_caches_for_user(
            user_mst_code=user_mst_code,
            tenants_mst_code=tenants_mst_code
        )

        # Extract unique service codes from all permissions
        service_codes = set()
        for cache in all_caches:
            if cache.permissions:
                for perm in cache.permissions:
                    if perm.get("service_mst_code"):
                        service_codes.add(perm["service_mst_code"])

        return list(service_codes)

    async def get_allowed_environments(
        self,
        user_mst_code: str,
        tenants_mst_code: str,
        service_mst_code: str
    ) -> List[str]:
        """
        Get list of environments user has permission for a specific service.

        Used to filter environment dropdown inside a service.

        Args:
            user_mst_code: User's code
            tenants_mst_code: Tenant code
            service_mst_code: Service to check

        Returns:
            List of environment values (e.g., ['dev', 'staging'])
        """
        # Get all cache rows for user (all environments)
        all_caches = await self.cache_repo.get_all_caches_for_user(
            user_mst_code=user_mst_code,
            tenants_mst_code=tenants_mst_code
        )

        # Find environments where user has permission for this service
        environments = []
        for cache in all_caches:
            if cache.permissions:
                for perm in cache.permissions:
                    if perm.get("service_mst_code") == service_mst_code:
                        # User has permission in this environment
                        env_value = cache.environment.value if cache.environment else None
                        if env_value and env_value not in environments:
                            environments.append(env_value)
                        break

        return environments

    async def rebuild_cache_for_role(
        self,
        role_type_ref_code: str,
        tenants_mst_code: str
    ) -> int:
        """
        Rebuild cache for all users who have a specific role.

        Args:
            role_type_ref_code: Role code (e.g., "developer")
            tenants_mst_code: Tenant code

        Returns:
            Number of users whose cache was rebuilt
        """
        logger.info(f"[CACHE_SVC] rebuild_cache_for_role: role={role_type_ref_code}, tenant={tenants_mst_code}")

        # Find all users with this role
        stmt = select(RoleMst.user_mst_id).where(
            RoleMst.role_type_ref_code == role_type_ref_code,
            RoleMst.is_deleted == False,
            RoleMst.is_active == True
        ).distinct()
        result = await self.session.execute(stmt)
        user_ids = [row[0] for row in result.fetchall()]

        logger.info(f"[CACHE_SVC] Found {len(user_ids)} users with role {role_type_ref_code}")

        rebuilt_count = 0
        for user_id in user_ids:
            # Get user by ID
            stmt = select(UserMstModel).where(UserMstModel.id == user_id)
            result = await self.session.execute(stmt)
            user = result.scalar_one_or_none()

            if user and user.tenants_mst_code == tenants_mst_code:
                await self.rebuild_cache(
                    user_mst_code=user.code,
                    tenants_mst_code=tenants_mst_code
                )
                rebuilt_count += 1

        logger.info(f"[CACHE_SVC] Rebuilt cache for {rebuilt_count} users")
        return rebuilt_count
