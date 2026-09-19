"""
Service User Permission Repository

Handles database operations for service_user_permission table.
Used to read raw permissions for cache building.
"""

from typing import List, Optional, Dict, Any
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.service_user_permission_model import ServiceUserPermissionModel
from app.db.models.services_mst_model import ServicesMstModel
from app.core.enum import EnvironmentEnum
import uuid


class ServiceUserPermissionRepository:
    """Repository for service_user_permission table operations."""

    def __init__(self, session: AsyncSession):
        """Initialize with database session."""
        self.session = session
        self.model = ServiceUserPermissionModel

    async def get_by_user(
        self,
        user_mst_code: str,
        tenants_mst_code: str,
        environment: Optional[EnvironmentEnum] = None
    ) -> List[ServiceUserPermissionModel]:
        """
        Get all direct permissions for a user.

        Args:
            user_mst_code: User's code
            tenants_mst_code: Tenant code for isolation
            environment: Optional environment filter

        Returns:
            List of permission rows assigned directly to user
        """
        filters = [
            self.model.user_mst_code == user_mst_code,
            self.model.tenants_mst_code == tenants_mst_code,
            self.model.is_deleted == False,
            self.model.is_active == True,
        ]

        # Add environment filter if specified
        if environment:
            # Include permissions for specific env OR all envs (NULL)
            filters.append(
                or_(
                    self.model.environment == environment,
                    self.model.environment == None
                )
            )

        stmt = select(self.model).where(and_(*filters))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_role_types(
        self,
        role_type_codes: List[str],
        tenants_mst_code: str,
        environment: Optional[EnvironmentEnum] = None
    ) -> List[ServiceUserPermissionModel]:
        """
        Get all permissions assigned to a list of role types.

        Args:
            role_type_codes: List of role type codes (from role_type_ref)
            tenants_mst_code: Tenant code for isolation
            environment: Optional environment filter

        Returns:
            List of permission rows assigned to those role types
        """
        if not role_type_codes:
            return []

        filters = [
            self.model.role_type_ref_code.in_(role_type_codes),
            self.model.tenants_mst_code == tenants_mst_code,
            self.model.is_deleted == False,
            self.model.is_active == True,
        ]

        if environment:
            filters.append(
                or_(
                    self.model.environment == environment,
                    self.model.environment == None
                )
            )

        stmt = select(self.model).where(and_(*filters))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ============ CRUD Methods for Permission Management ============

    async def create_permission(
        self,
        tenants_mst_code: str,
        data: Dict[str, Any]
    ) -> ServiceUserPermissionModel:
        """
        Create a new permission (or return existing if duplicate).

        Duplicate check:
        1. Same user/role + same service/app + same environment + same policy
        2. For service-level: Also check if application-level permission exists
           that covers this service (same user/role + parent app + same env + same policy)

        Args:
            tenants_mst_code: Tenant code
            data: Permission data dict

        Returns:
            Created (or existing) permission model
        """
        # Check for existing permission with same parameters
        env_value = EnvironmentEnum(data["environment"]) if data.get("environment") else None

        filters = [
            self.model.tenants_mst_code == tenants_mst_code,
            self.model.policy_ref_code == data["policy_ref_code"],
            self.model.is_deleted == False,
        ]

        # User or Role filter
        if data.get("user_mst_code"):
            filters.append(self.model.user_mst_code == data["user_mst_code"])
        elif data.get("role_type_ref_code"):
            filters.append(self.model.role_type_ref_code == data["role_type_ref_code"])

        # Service or Application filter
        if data.get("services_mst_code"):
            filters.append(self.model.services_mst_code == data["services_mst_code"])
        elif data.get("applications_mst_code"):
            filters.append(self.model.applications_mst_code == data["applications_mst_code"])

        # Environment filter
        if env_value:
            filters.append(self.model.environment == env_value)
        else:
            filters.append(self.model.environment.is_(None))

        stmt = select(self.model).where(and_(*filters))
        result = await self.session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # Return existing permission instead of creating duplicate
            return existing

        # Additional check for service-level permissions:
        # Block if application-level permission already covers this service
        if data.get("services_mst_code"):
            # Look up the service to get its parent application
            svc_stmt = select(ServicesMstModel).where(
                ServicesMstModel.code == data["services_mst_code"]
            )
            svc_result = await self.session.execute(svc_stmt)
            service = svc_result.scalar_one_or_none()

            if service and service.applications_mst_code:
                # Check if application-level permission exists
                app_filters = [
                    self.model.tenants_mst_code == tenants_mst_code,
                    self.model.policy_ref_code == data["policy_ref_code"],
                    self.model.applications_mst_code == service.applications_mst_code,
                    self.model.services_mst_code.is_(None),  # App-level has no service
                    self.model.is_deleted == False,
                ]

                # Same user or role filter
                if data.get("user_mst_code"):
                    app_filters.append(self.model.user_mst_code == data["user_mst_code"])
                elif data.get("role_type_ref_code"):
                    app_filters.append(self.model.role_type_ref_code == data["role_type_ref_code"])

                # Same environment filter
                if env_value:
                    app_filters.append(self.model.environment == env_value)
                else:
                    app_filters.append(self.model.environment.is_(None))

                app_stmt = select(self.model).where(and_(*app_filters))
                app_result = await self.session.execute(app_stmt)
                app_level_perm = app_result.scalar_one_or_none()

                if app_level_perm:
                    # Application-level permission exists - return it instead of creating service-level
                    return app_level_perm

        # Create new permission
        permission = self.model(
            code=f"SUP_{uuid.uuid4().hex[:12]}",
            name=data.get("name", "Service Permission"),
            tenants_mst_code=tenants_mst_code,
            user_mst_code=data.get("user_mst_code"),
            role_type_ref_code=data.get("role_type_ref_code"),
            policy_ref_code=data["policy_ref_code"],
            services_mst_code=data.get("services_mst_code"),
            applications_mst_code=data.get("applications_mst_code"),
            resource_group_mst_code=data.get("resource_group_mst_code"),
            environment=env_value,
            is_active=True,
            is_deleted=False
        )

        self.session.add(permission)
        await self.session.flush()
        return permission

    async def delete_permission(
        self,
        code: str,
        tenants_mst_code: str
    ) -> bool:
        """
        Soft delete a permission.

        Args:
            code: Permission code
            tenants_mst_code: Tenant code

        Returns:
            True if deleted, False if not found
        """
        stmt = select(self.model).where(
            and_(
                self.model.code == code,
                self.model.tenants_mst_code == tenants_mst_code,
                self.model.is_deleted == False
            )
        )
        result = await self.session.execute(stmt)
        permission = result.scalar_one_or_none()

        if not permission:
            return False

        permission.is_deleted = True
        await self.session.flush()
        return True

    async def get_by_scope(
        self,
        tenants_mst_code: str,
        scope_type: str,
        scope_code: str
    ) -> List[ServiceUserPermissionModel]:
        """
        Get permissions for a specific scope (service or application).

        Args:
            tenants_mst_code: Tenant code
            scope_type: "service" or "application"
            scope_code: Service or Application code

        Returns:
            List of permissions
        """
        filters = [
            self.model.tenants_mst_code == tenants_mst_code,
            self.model.is_deleted == False,
            self.model.is_active == True,
        ]

        if scope_type == "service":
            filters.append(self.model.services_mst_code == scope_code)
        elif scope_type == "application":
            filters.append(self.model.applications_mst_code == scope_code)

        stmt = select(self.model).where(and_(*filters))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_all_for_tenant(
        self,
        tenants_mst_code: str
    ) -> List[ServiceUserPermissionModel]:
        """
        Get all permissions for a tenant.

        Args:
            tenants_mst_code: Tenant code

        Returns:
            List of all permissions
        """
        stmt = select(self.model).where(
            and_(
                self.model.tenants_mst_code == tenants_mst_code,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_code(
        self,
        code: str,
        tenants_mst_code: str
    ) -> Optional[ServiceUserPermissionModel]:
        """
        Get a permission by code.

        Args:
            code: Permission code
            tenants_mst_code: Tenant code

        Returns:
            Permission model or None
        """
        stmt = select(self.model).where(
            and_(
                self.model.code == code,
                self.model.tenants_mst_code == tenants_mst_code,
                self.model.is_deleted == False
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_assignee_and_services(
        self,
        tenants_mst_code: str,
        assignee_type: str,
        assignee_code: str,
        service_codes: List[str]
    ) -> List[ServiceUserPermissionModel]:
        """
        Get permissions for a user/role filtered by service codes.

        Args:
            tenants_mst_code: Tenant code
            assignee_type: "user" or "role"
            assignee_code: User or Role code
            service_codes: List of service codes to filter by

        Returns:
            List of permissions
        """
        filters = [
            self.model.tenants_mst_code == tenants_mst_code,
            self.model.is_deleted == False,
            self.model.is_active == True,
            self.model.services_mst_code.in_(service_codes),
        ]

        if assignee_type == "user":
            filters.append(self.model.user_mst_code == assignee_code)
        else:
            filters.append(self.model.role_type_ref_code == assignee_code)

        stmt = select(self.model).where(and_(*filters))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_role_and_application(
        self,
        tenants_mst_code: str,
        role_type_ref_code: str,
        applications_mst_code: str
    ) -> List[ServiceUserPermissionModel]:
        """
        Get application-level permissions for a role.

        These are permissions where applications_mst_code is set
        (services_mst_code is NULL), meaning the role has access
        to ALL services in that application.

        Args:
            tenants_mst_code: Tenant code
            role_type_ref_code: Role code
            applications_mst_code: Application code

        Returns:
            List of permissions
        """
        stmt = select(self.model).where(
            and_(
                self.model.tenants_mst_code == tenants_mst_code,
                self.model.role_type_ref_code == role_type_ref_code,
                self.model.applications_mst_code == applications_mst_code,
                self.model.is_deleted == False,
                self.model.is_active == True,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_user_and_application(
        self,
        tenants_mst_code: str,
        user_mst_code: str,
        applications_mst_code: str
    ) -> List[ServiceUserPermissionModel]:
        """
        Get application-level permissions for a user.

        These are permissions where applications_mst_code is set
        (services_mst_code is NULL), meaning the user has access
        to ALL services in that application.

        Args:
            tenants_mst_code: Tenant code
            user_mst_code: User code
            applications_mst_code: Application code

        Returns:
            List of permissions
        """
        stmt = select(self.model).where(
            and_(
                self.model.tenants_mst_code == tenants_mst_code,
                self.model.user_mst_code == user_mst_code,
                self.model.applications_mst_code == applications_mst_code,
                self.model.is_deleted == False,
                self.model.is_active == True,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
