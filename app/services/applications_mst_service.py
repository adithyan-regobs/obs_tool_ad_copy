from typing import Dict, Any, List, Optional
import logging
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.db.models.workspace_user_map_model import WorkspaceUserMapModel
from app.repository.applications_mst_repository import ApplicationsMstRepository
from app.services.workspace_service import WorkspaceService
from app.repository.tenants_mst_repository import TenantsMstRepository
from app.repository.resource_group_mst_repository import ResourceGroupMstRepository
from app.domain.validators.applications_mst_rules import ApplicationsMstValidator, ApplicationValidationError
from app.domain.factories.applications_mst_factory import make_application
from app.domain.factories.resource_group_mst_factory import make_default_resource_group
from app.schemas.application_schemas import CreateApplicationRequest

logger = logging.getLogger(__name__)


class ApplicationsMstService:
    """
    Service layer for Applications Master operations.
    Handles business logic and orchestrates repository calls.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.applications_repository = ApplicationsMstRepository(session)
        self.tenants_repository = TenantsMstRepository(session)
        self.resource_groups_repository = ResourceGroupMstRepository(session)

    async def can_write_for_app(
        self,
        user_code: str,
        tenant_code: str,
        application_code: str,
    ) -> bool:
        """Returns True if user has owner/admin/edit role (not read_only) for the workspace that owns the application."""
        from app.db.models.applications_mst_model import ApplicationsMstModel
        stmt = select(ApplicationsMstModel.workspace_code).where(
            ApplicationsMstModel.code == application_code,
            ApplicationsMstModel.is_deleted == False,
        )
        result = await self.session.execute(stmt)
        workspace_code = result.scalar_one_or_none()
        if not workspace_code:
            return False
        workspace_svc = WorkspaceService(self.session)
        return await workspace_svc.can_write_for_workspace(user_code, tenant_code, workspace_code)

    async def _has_workspace_access(self, user_code: str, workspace_code: str, tenant_code: str) -> bool:
        stmt = (
            select(WorkspaceUserMapModel)
            .where(
                WorkspaceUserMapModel.workspace_code == workspace_code,
                WorkspaceUserMapModel.user_mst_code == user_code,
                WorkspaceUserMapModel.tenants_mst_code == tenant_code,
                WorkspaceUserMapModel.is_active == True,
                WorkspaceUserMapModel.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get_all_applications(
        self,
        tenant_code: str,
        user_code: str,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: int = 100,
        workspace_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get all applications with complete information.

        Business Logic:
        - Enforces tenant isolation
        - Validates pagination parameters
        - Aggregates application data from multiple sources

        Args:
            tenant_code: Tenant code for isolation
            is_active: Optional status filter
            skip: Pagination offset
            limit: Page size

        Returns:
            Dict with total count and list of applications

        Raises:
            ApplicationValidationError: If validation fails
        """
        logger.info(
            f"Fetching applications for tenant {tenant_code}",
            extra={"tenant_code": tenant_code, "is_active": is_active}
        )
        logger.debug(f"Pagination: skip={skip}, limit={limit}")

        # Business logic: Validate request using domain validators
        try:
            ApplicationsMstValidator.validate_get_all_applications_request(
                tenant_code=tenant_code,
                skip=skip,
                limit=limit
            )
        except ApplicationValidationError as e:
            logger.warning(f"Validation failed for get_all_applications: {str(e)}")
            raise

        workspace_svc = WorkspaceService(self.session)
        accessible_workspace_codes = None

        if workspace_code:
            has_access = await self._has_workspace_access(user_code, workspace_code, tenant_code)
            if not has_access:
                logger.warning(
                    f"User {user_code} has no access to workspace {workspace_code}",
                    extra={"user_code": user_code, "workspace_code": workspace_code}
                )
                return {"total": 0, "applications": []}
        else:
            accessible_workspace_codes = await workspace_svc.get_user_workspace_codes(user_code, tenant_code)
            if not accessible_workspace_codes:
                return {"total": 0, "applications": []}

        # Call repository layer
        result = await self.applications_repository.get_all_applications(
            tenant_code=tenant_code,
            is_active=is_active,
            skip=skip,
            limit=limit,
            workspace_code=workspace_code,
            workspace_codes=accessible_workspace_codes,
        )

        logger.info(
            f"Successfully retrieved {result['total']} applications for tenant {tenant_code}",
            extra={"tenant_code": tenant_code, "count": result['total']}
        )

        # Business logic: Additional processing if needed
        # (e.g., calculate derived fields, apply business rules)

        return result

    async def get_applications_dropdown(
        self,
        tenant_code: str,
        user_code: str,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: int = 100,
        workspace_code: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """
        Get all applications for a tenant as `[{label, value}]` dropdown
        options where `value` is the application code and `label` is the
        application name.

        Calls the same repository method as `get_all_applications`, applies
        validation + the `is_active` filter, then transforms the rows into
        the standardized dropdown shape used across the app.

        Args:
            tenant_code: Tenant code for isolation
            is_active: Optional status filter
            skip: Pagination offset
            limit: Page size

        Returns:
            List of `{label, value}` dropdown options

        Raises:
            ApplicationValidationError: If validation fails
        """
        logger.info(
            f"Fetching applications dropdown for tenant {tenant_code}",
            extra={"tenant_code": tenant_code, "is_active": is_active},
        )
        logger.debug(f"Pagination: skip={skip}, limit={limit}")

        # Business logic: Validate request using domain validators
        try:
            ApplicationsMstValidator.validate_get_all_applications_request(
                tenant_code=tenant_code,
                skip=skip,
                limit=limit,
            )
        except ApplicationValidationError as e:
            logger.warning(f"Validation failed for get_applications_dropdown: {str(e)}")
            raise

        workspace_svc = WorkspaceService(self.session)
        accessible_workspace_codes = None

        if workspace_code:
            has_access = await self._has_workspace_access(user_code, workspace_code, tenant_code)
            if not has_access:
                logger.warning(
                    f"User {user_code} has no access to workspace {workspace_code}",
                    extra={"user_code": user_code, "workspace_code": workspace_code}
                )
                return []
        else:
            accessible_workspace_codes = await workspace_svc.get_user_workspace_codes(user_code, tenant_code)
            if not accessible_workspace_codes:
                return []

        # Call the same repository method used by get_all_applications
        result = await self.applications_repository.get_all_applications(
            tenant_code=tenant_code,
            is_active=is_active,
            skip=skip,
            limit=limit,
            workspace_code=workspace_code,
            workspace_codes=accessible_workspace_codes,
        )

        applications = result.get("applications", [])
        logger.info(
            f"Successfully retrieved {len(applications)} applications "
            f"for dropdown (tenant {tenant_code})",
            extra={"tenant_code": tenant_code, "count": len(applications)},
        )

        return [
            {
                "label": app.get("application_name"),
                "value": app.get("application_code"),
            }
            for app in applications
        ]

    async def create_application(
        self,
        tenant_subdomain: str,
        data: CreateApplicationRequest,
        user_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create application and automatically create default resource group.

        Business Logic:
        1. Lookup tenant by subdomain (from JWT authentication)
        2. Create application with auto-generated code
        3. Auto-create default resource group
        4. Both creates are atomic (single transaction)

        Args:
            tenant_subdomain: Tenant subdomain (from JWT authentication)
            data: CreateApplicationRequest with application_name, description

        Returns:
            Dict with application and resource group details

        Raises:
            ApplicationValidationError: Validation failed
            ValueError: Tenant not found
        """
        logger.info(
            f"Creating application '{data.application_name}' for tenant subdomain '{tenant_subdomain}'",
            extra={"tenant_subdomain": tenant_subdomain, "application_name": data.application_name}
        )

        # Step 1: Validate request
        try:
            ApplicationsMstValidator.validate_create_application_request(
                tenant=tenant_subdomain,
                application_name=data.application_name
            )
        except ApplicationValidationError as e:
            logger.warning(f"Validation failed for create_application: {str(e)}")
            raise

        # Step 2: Lookup tenant by subdomain (from JWT authentication)
        logger.debug(f"Looking up tenant by subdomain: {tenant_subdomain}")
        tenant = await self.tenants_repository.get_by_subdomain(tenant_subdomain)
        if not tenant:
            logger.error(f"Tenant not found: {tenant_subdomain}")
            raise ValueError(f"Tenant '{tenant_subdomain}' not found")

        logger.debug(f"Found tenant: {tenant.code} ({tenant.name})")

        # Write-access guard: block read_only role from creating applications
        if user_code and data.workspace_code:
            workspace_svc = WorkspaceService(self.session)
            if not await workspace_svc.can_write_for_workspace(user_code, tenant.code, data.workspace_code):
                raise ValueError("Access denied: read-only role cannot create applications")

        # Step 3: Create application using factory
        logger.debug(f"Creating application with code generation")
        application_data = make_application(
            tenant_code=tenant.code,
            application_name=data.application_name,
            description=data.description,
            workspace_code=data.workspace_code,
        )
        created_app = await self.applications_repository.create(**application_data)
        logger.info(
            f"Application created - ID: {created_app.id}, Code: {created_app.code}",
            extra={"application_id": created_app.id, "application_code": created_app.code}
        )

        # Step 4: Create default resource group
        logger.debug(f"Creating default resource group for application {created_app.code}")
        resource_group_data = make_default_resource_group(
            application_code=created_app.code,
            application_name=data.application_name,
            tenant_code=tenant.code
        )
        created_rg = await self.resource_groups_repository.create(**resource_group_data)
        logger.info(
            f"Default resource group created - Code: {created_rg.code}",
            extra={"resource_group_code": created_rg.code}
        )

        logger.info(
            f"Successfully created application '{data.application_name}' with default resource group",
            extra={
                "application_code": created_app.code,
                "resource_group_code": created_rg.code,
                "tenant_code": tenant.code
            }
        )

        # Step 5: Return response
        return {
            # Application details
            'id': created_app.id,
            'code': created_app.code,
            'name': created_app.name,
            'description': created_app.description,
            'tenant_code': tenant.code,
            'tenant_name': tenant.name,
            'is_active': created_app.is_active,
            'created_at': created_app.created_at,

            'workspace_code': created_app.workspace_code,

            # Default resource group details
            'default_resource_group_code': created_rg.code,
            'default_resource_group_name': created_rg.name,

            'message': 'Application created successfully with default resource group'
        }
