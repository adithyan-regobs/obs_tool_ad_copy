"""
Permission Management Service

Business logic for managing service permissions from the UI.
Handles CRUD operations and lookups for the permission management page.
"""

from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.repository.service_user_permission_repository import ServiceUserPermissionRepository
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.policy_ref_model import PolicyRefModel
from app.db.models.role_type_ref_model import RoleTypeRefModel
from app.utils.permission_helper import PermissionHelper
from app.schemas.permission_schemas import (
    ServicePermissionCreate,
    ServicePermissionResponse,
    ManageableScope,
    Assignee,
    PolicyOption
)


class PermissionManagementService:
    """Service for permission management UI operations."""

    def __init__(self, session: AsyncSession):
        """Initialize with database session."""
        self.session = session
        self.perm_repo = ServiceUserPermissionRepository(session)
        self.perm_helper = PermissionHelper(session)

    async def get_manageable_scopes(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel
    ) -> List[ManageableScope]:
        """
        Get services and applications user can manage permissions for.

        For org_owner: Returns all services and applications.
        For service_admin: Returns services where user has service_admin.

        Args:
            user: Current user
            tenant: Current tenant

        Returns:
            List of ManageableScope objects
        """
        scopes = []

        if await self.perm_helper.is_org_owner(user):
            # Org owner can manage all - get all apps and services
            scopes.extend(await self._get_all_applications(tenant.code))
            scopes.extend(await self._get_all_services(tenant.code))
        else:
            # Get services where user has can_manage permission
            scopes.extend(await self._get_manageable_services(user, tenant))

        return scopes

    async def _get_all_applications(self, tenant_code: str) -> List[ManageableScope]:
        """Get all applications for tenant."""
        stmt = select(ApplicationsMstModel).where(
            ApplicationsMstModel.tenants_mst_code == tenant_code,
            ApplicationsMstModel.is_deleted == False
        )
        result = await self.session.execute(stmt)
        apps = result.scalars().all()

        return [
            ManageableScope(
                code=app.code,
                name=app.name,
                scope_type="application"
            )
            for app in apps
        ]

    async def _get_all_services(self, tenant_code: str) -> List[ManageableScope]:
        """Get all services for tenant."""
        stmt = select(ServicesMstModel).where(
            ServicesMstModel.tenants_mst_code == tenant_code,
            ServicesMstModel.is_deleted == False
        )
        result = await self.session.execute(stmt)
        services = result.scalars().all()

        return [
            ManageableScope(
                code=svc.code,
                name=svc.name,
                scope_type="service"
            )
            for svc in services
        ]

    async def _get_manageable_services(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel
    ) -> List[ManageableScope]:
        """Get services where user has can_manage permission."""
        # Get ALL cache rows (across all environments)
        all_caches = await self.perm_helper.cache_service.cache_repo.get_all_caches_for_user(
            user_mst_code=user.code,
            tenants_mst_code=tenant.code
        )

        if not all_caches:
            return []

        # Get service codes where user has can_manage from ANY environment
        service_codes = set()
        for cache in all_caches:
            if cache.permissions:
                for perm in cache.permissions:
                    if perm.get("can_manage", False):
                        service_codes.add(perm.get("service_mst_code"))

        if not service_codes:
            return []

        # Fetch service details
        stmt = select(ServicesMstModel).where(
            ServicesMstModel.code.in_(service_codes),
            ServicesMstModel.is_deleted == False
        )
        result = await self.session.execute(stmt)
        services = result.scalars().all()

        return [
            ManageableScope(
                code=svc.code,
                name=svc.name,
                scope_type="service"
            )
            for svc in services
        ]

    async def get_manageable_applications(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel
    ) -> List[Dict[str, Any]]:
        """
        Get applications the user can assign permissions for.

        For org_owner: Returns all applications.
        For service_admin: Returns applications containing services they can manage.
        """
        if await self.perm_helper.is_org_owner(user):
            # Org owner can see all applications
            stmt = select(ApplicationsMstModel).where(
                ApplicationsMstModel.tenants_mst_code == tenant.code,
                ApplicationsMstModel.is_deleted == False
            )
            result = await self.session.execute(stmt)
            apps = result.scalars().all()
            return [{"code": app.code, "name": app.name} for app in apps]

        # Get service codes user can manage
        all_caches = await self.perm_helper.cache_service.cache_repo.get_all_caches_for_user(
            user_mst_code=user.code,
            tenants_mst_code=tenant.code
        )

        service_codes = set()
        for cache in all_caches:
            if cache.permissions:
                for perm in cache.permissions:
                    if perm.get("can_manage", False):
                        service_codes.add(perm.get("service_mst_code"))

        if not service_codes:
            return []

        # Get applications that contain these services
        stmt = select(ApplicationsMstModel).join(
            ServicesMstModel,
            ServicesMstModel.applications_mst_code == ApplicationsMstModel.code
        ).where(
            ServicesMstModel.code.in_(service_codes),
            ApplicationsMstModel.is_deleted == False
        ).distinct()
        result = await self.session.execute(stmt)
        apps = result.scalars().all()

        return [{"code": app.code, "name": app.name} for app in apps]

    async def get_manageable_services_for_application(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel,
        application_code: str
    ) -> List[Dict[str, Any]]:
        """
        Get services within an application that user can assign permissions for.

        For org_owner: Returns all services in the application.
        For service_admin: Returns only services they can manage.
        """
        if await self.perm_helper.is_org_owner(user):
            # Org owner can see all services in the application
            stmt = select(ServicesMstModel).where(
                ServicesMstModel.tenants_mst_code == tenant.code,
                ServicesMstModel.applications_mst_code == application_code,
                ServicesMstModel.is_deleted == False
            )
            result = await self.session.execute(stmt)
            services = result.scalars().all()
            return [
                {
                    "code": svc.code,
                    "name": svc.name,
                    "service_type": svc.service_type
                }
                for svc in services
            ]

        # Get service codes user can manage
        all_caches = await self.perm_helper.cache_service.cache_repo.get_all_caches_for_user(
            user_mst_code=user.code,
            tenants_mst_code=tenant.code
        )

        service_codes = set()
        for cache in all_caches:
            if cache.permissions:
                for perm in cache.permissions:
                    if perm.get("can_manage", False):
                        service_codes.add(perm.get("service_mst_code"))

        if not service_codes:
            return []

        # Get services in this application that user can manage
        stmt = select(ServicesMstModel).where(
            ServicesMstModel.code.in_(service_codes),
            ServicesMstModel.applications_mst_code == application_code,
            ServicesMstModel.is_deleted == False
        )
        result = await self.session.execute(stmt)
        services = result.scalars().all()

        return [
            {
                "code": svc.code,
                "name": svc.name,
                "service_type": svc.service_type
            }
            for svc in services
        ]

    async def get_all_permissions(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel
    ) -> List[ServicePermissionResponse]:
        """
        Get all permissions the user can manage.

        For org_owner: Returns all permissions for the tenant.
        For service_admin: Returns permissions for services they can manage.

        Args:
            user: Current user
            tenant: Current tenant

        Returns:
            List of ServicePermissionResponse objects
        """
        # Get manageable scopes first
        scopes = await self.get_manageable_scopes(user, tenant)

        result = []
        for scope in scopes:
            scope_permissions = await self.get_permissions_for_scope(
                tenant, scope.scope_type, scope.code
            )
            result.extend(scope_permissions)

        return result

    async def get_permissions_for_scope(
        self,
        tenant: TenantsMstModel,
        scope_type: str,
        scope_code: str
    ) -> List[ServicePermissionResponse]:
        """
        Get all permissions for a specific scope.

        Args:
            tenant: Current tenant
            scope_type: "service" or "application"
            scope_code: Service or Application code

        Returns:
            List of ServicePermissionResponse objects
        """
        permissions = await self.perm_repo.get_by_scope(
            tenants_mst_code=tenant.code,
            scope_type=scope_type,
            scope_code=scope_code
        )

        # Build response with lookups
        result = []
        for perm in permissions:
            response = await self._build_permission_response(perm, scope_type, scope_code)
            if response:
                result.append(response)

        return result

    async def _build_permission_response(
        self,
        perm: Any,
        scope_type: str,
        scope_code: str
    ) -> Optional[ServicePermissionResponse]:
        """Build ServicePermissionResponse from permission model."""
        # Determine assignment type and get assignee info
        if perm.user_mst_code:
            assignment_type = "user"
            assignee_code = perm.user_mst_code
            assignee_name = await self._get_user_name(perm.user_mst_code)
        elif perm.role_type_ref_code:
            assignment_type = "role"
            assignee_code = perm.role_type_ref_code
            assignee_name = await self._get_role_name(perm.role_type_ref_code)
        else:
            return None

        # Get policy info
        policy = await self._get_policy(perm.policy_ref_code)
        if not policy:
            return None

        # Get scope name
        scope_name = await self._get_scope_name(scope_type, scope_code)

        return ServicePermissionResponse(
            code=perm.code,
            assignment_type=assignment_type,
            assignee_name=assignee_name or "Unknown",
            assignee_code=assignee_code,
            policy_ref_code=perm.policy_ref_code,
            policy_name=policy.name,
            scope_type=scope_type,
            scope_name=scope_name or "Unknown",
            scope_code=scope_code,
            environment=perm.environment.value if perm.environment else None,
            can_read=policy.can_read,
            can_write=policy.can_write,
            can_manage=policy.can_manage
        )

    async def create_permission(
        self,
        tenant: TenantsMstModel,
        data: ServicePermissionCreate
    ) -> ServicePermissionResponse:
        """
        Create a new permission.

        Args:
            tenant: Current tenant
            data: Permission creation data

        Returns:
            Created permission response
        """
        # Build data dict for repository
        perm_data = {
            "user_mst_code": data.user_mst_code if data.assignment_type == "user" else None,
            "role_type_ref_code": data.role_type_ref_code if data.assignment_type == "role" else None,
            "policy_ref_code": data.policy_ref_code,
            "services_mst_code": data.services_mst_code if data.scope_type == "service" else None,
            "applications_mst_code": data.applications_mst_code if data.scope_type == "application" else None,
            "environment": data.environment
        }

        permission = await self.perm_repo.create_permission(
            tenants_mst_code=tenant.code,
            data=perm_data
        )

        # Build and return response
        scope_code = data.services_mst_code or data.applications_mst_code
        return await self._build_permission_response(permission, data.scope_type, scope_code)

    async def delete_permission(
        self,
        tenant: TenantsMstModel,
        code: str
    ) -> bool:
        """
        Delete a permission.

        Args:
            tenant: Current tenant
            code: Permission code to delete

        Returns:
            True if deleted, False if not found
        """
        return await self.perm_repo.delete_permission(
            code=code,
            tenants_mst_code=tenant.code
        )

    async def get_assignees(
        self,
        tenant: TenantsMstModel,
        current_user: UserMstModel
    ) -> Dict[str, List[Assignee]]:
        """
        Get available users and roles for assignment.

        Args:
            tenant: Current tenant
            current_user: Currently logged-in user (to exclude from list)

        Returns:
            Dict with 'users' and 'roles' lists
        """
        # Get users - exclude org_owners (they have full access) and current user
        stmt = select(UserMstModel).where(
            UserMstModel.tenants_mst_code == tenant.code,
            UserMstModel.is_deleted == False,
            UserMstModel.is_active == True,
            UserMstModel.is_org_owner != True,  # Exclude org_owners
            UserMstModel.code != current_user.code  # Exclude current user
        )
        result = await self.session.execute(stmt)
        users = result.scalars().all()

        user_list = [
            Assignee(
                type="user",
                code=u.code,
                name=u.name or u.email_id,
                email=u.email_id
            )
            for u in users
        ]

        # Get roles
        stmt = select(RoleTypeRefModel).where(
            RoleTypeRefModel.is_deleted == False,
            RoleTypeRefModel.is_active == True
        )
        result = await self.session.execute(stmt)
        roles = result.scalars().all()

        role_list = [
            Assignee(
                type="role",
                code=r.code,
                name=r.name
            )
            for r in roles
        ]

        return {"users": user_list, "roles": role_list}

    async def get_policies(self) -> List[PolicyOption]:
        """
        Get available policies for dropdown.

        Returns:
            List of PolicyOption objects
        """
        stmt = select(PolicyRefModel).where(
            PolicyRefModel.is_deleted == False,
            PolicyRefModel.is_active == True
        )
        result = await self.session.execute(stmt)
        policies = result.scalars().all()

        return [
            PolicyOption(
                code=p.code,
                name=p.name,
                description=p.description,
                can_read=p.can_read,
                can_write=p.can_write,
                can_manage=p.can_manage
            )
            for p in policies
        ]

    # ============ Lookup Helpers ============

    async def _get_user_name(self, user_code: str) -> Optional[str]:
        """Get user name by code."""
        stmt = select(UserMstModel).where(UserMstModel.code == user_code)
        result = await self.session.execute(stmt)
        user = result.scalar_one_or_none()
        return user.name or user.email_id if user else None

    async def _get_role_name(self, role_code: str) -> Optional[str]:
        """Get role name by code."""
        stmt = select(RoleTypeRefModel).where(RoleTypeRefModel.code == role_code)
        result = await self.session.execute(stmt)
        role = result.scalar_one_or_none()
        return role.name if role else None

    async def _get_policy(self, policy_code: str) -> Optional[PolicyRefModel]:
        """Get policy by code."""
        stmt = select(PolicyRefModel).where(PolicyRefModel.code == policy_code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_scope_name(self, scope_type: str, scope_code: str) -> Optional[str]:
        """Get scope name by type and code."""
        if scope_type == "service":
            stmt = select(ServicesMstModel).where(ServicesMstModel.code == scope_code)
            result = await self.session.execute(stmt)
            svc = result.scalar_one_or_none()
            return svc.name if svc else None
        elif scope_type == "application":
            stmt = select(ApplicationsMstModel).where(ApplicationsMstModel.code == scope_code)
            result = await self.session.execute(stmt)
            app = result.scalar_one_or_none()
            return app.name if app else None
        return None

    async def get_existing_permissions_for_assignee(
        self,
        tenant: TenantsMstModel,
        assignee_type: str,
        assignee_code: str,
        application_code: str
    ) -> Dict[str, Any]:
        """
        Get existing permissions for a user/role within an application.

        For users: Read from cache table (has flattened permissions including role-based).
        For roles: Read from permission table (roles don't have cache).

        Returns a dict mapping service_code -> list of environments,
        plus application_level_environments for app-level permissions.

        Args:
            tenant: Current tenant
            assignee_type: "user" or "role"
            assignee_code: User or Role code
            application_code: Application code

        Returns:
            Dict with service_code keys and environment list values,
            plus application_level_environments list
        """
        # Get services in this application
        stmt = select(ServicesMstModel).where(
            ServicesMstModel.applications_mst_code == application_code,
            ServicesMstModel.tenants_mst_code == tenant.code,
            ServicesMstModel.is_deleted == False
        )
        result = await self.session.execute(stmt)
        services = result.scalars().all()
        service_codes = set(s.code for s in services)

        if not service_codes:
            return {"existing_permissions": {}, "application_level_environments": []}

        existing: Dict[str, List[str]] = {}
        app_level_envs: List[str] = []

        if assignee_type == "user":
            # For users: Read from cache table (includes role-based permissions)
            all_caches = await self.perm_helper.cache_service.cache_repo.get_all_caches_for_user(
                user_mst_code=assignee_code,
                tenants_mst_code=tenant.code
            )

            for cache in all_caches:
                if cache.permissions:
                    env = cache.environment.value if cache.environment else None
                    for perm in cache.permissions:
                        svc_code = perm.get("service_mst_code")
                        if svc_code and svc_code in service_codes:
                            if svc_code not in existing:
                                existing[svc_code] = []
                            if env and env not in existing[svc_code]:
                                existing[svc_code].append(env)

            # Also check for application-level permissions directly from permission table
            app_perms = await self.perm_repo.get_by_user_and_application(
                tenants_mst_code=tenant.code,
                user_mst_code=assignee_code,
                applications_mst_code=application_code
            )
            for perm in app_perms:
                env = perm.environment.value if perm.environment else None
                if env and env not in app_level_envs:
                    app_level_envs.append(env)

        else:
            # For roles: Read from permission table (roles don't have cache)
            # Check both service-level and application-level permissions

            # 1. Get service-level permissions
            service_perms = await self.perm_repo.get_by_assignee_and_services(
                tenants_mst_code=tenant.code,
                assignee_type=assignee_type,
                assignee_code=assignee_code,
                service_codes=list(service_codes)
            )

            for perm in service_perms:
                svc_code = perm.services_mst_code
                env = perm.environment.value if perm.environment else None
                if svc_code not in existing:
                    existing[svc_code] = []
                if env and env not in existing[svc_code]:
                    existing[svc_code].append(env)

            # 2. Get application-level permissions for this role
            app_perms = await self.perm_repo.get_by_role_and_application(
                tenants_mst_code=tenant.code,
                role_type_ref_code=assignee_code,
                applications_mst_code=application_code
            )

            # Application-level permissions apply to ALL services in that application
            for perm in app_perms:
                env = perm.environment.value if perm.environment else None
                if env and env not in app_level_envs:
                    app_level_envs.append(env)
                for svc_code in service_codes:
                    if svc_code not in existing:
                        existing[svc_code] = []
                    if env and env not in existing[svc_code]:
                        existing[svc_code].append(env)

        return {"existing_permissions": existing, "application_level_environments": app_level_envs}
