from typing import Dict, Any, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.repository.services_mst_repository import ServicesMstRepository
from app.repository.applications_mst_repository import ApplicationsMstRepository
from app.domain.validators.services_mst_rules import ServicesMstValidator, ServiceValidationError
from app.domain.factories.services_mst_factory import make_service
from app.schemas.service_schemas import CreateServiceRequest
from app.services.workspace_service import WorkspaceService
from app.services.applications_mst_service import ApplicationsMstService


class ServicesMstService:
    """
    Service layer for Services Master operations.
    Handles business logic and orchestrates repository calls.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.services_repository = ServicesMstRepository(session)
        self.applications_repository = ApplicationsMstRepository(session)

    async def get_all_services(
        self,
        tenant_code: str,
        user_code: str,
        application_code: Optional[str] = None,
        resource_group_mst_code: Optional[str] = None,
        is_active: Optional[bool] = None,
        search_query: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        allowed_service_codes: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Get all services with complete information.

        Business Logic:
        - Enforces tenant isolation
        - Validates pagination parameters
        - Filters by user permissions (allowed_service_codes)
        - Aggregates service data from multiple sources

        Args:
            tenant_code: Tenant code for isolation
            application_code: Optional application filter
            is_active: Optional status filter
            skip: Pagination offset
            limit: Page size
            allowed_service_codes: List of service codes user has permission for

        Returns:
            Dict with total count and list of services

        Raises:
            ServiceValidationError: If validation fails
        """
        # Business logic: Validate request using domain validators
        ServicesMstValidator.validate_get_all_services_request(
            tenant_code=tenant_code,
            application_code=application_code,
            skip=skip,
            limit=limit
        )

        workspace_svc = WorkspaceService(self.session)

        if application_code:
            # Single app requested — verify workspace access
            if not await workspace_svc.verify_app_workspace_access(user_code, tenant_code, application_code):
                return {"total": 0, "services": []}
            accessible_app_codes = None
        else:
            # No app filter — resolve accessible apps via user's workspaces
            workspace_codes = await workspace_svc.get_user_workspace_codes(user_code, tenant_code)
            accessible_app_codes = await self.applications_repository.get_codes_by_workspaces(
                workspace_codes, tenant_code
            )
            if not accessible_app_codes:
                return {"total": 0, "services": []}

        # Call repository layer
        result = await self.services_repository.get_all_services(
            tenant_code=tenant_code,
            application_code=application_code,
            application_codes=accessible_app_codes,
            resource_group_mst_code=resource_group_mst_code,
            is_active=is_active,
            search_query=search_query,
            skip=skip,
            limit=limit,
            allowed_service_codes=allowed_service_codes
        )

        # Business logic: Additional processing if needed
        # (e.g., calculate derived fields, apply business rules)

        return result

    async def get_service_detail(
        self,
        tenant_code: str,
        service_code: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get complete details for a single service.

        Business Logic:
        - Enforces tenant isolation
        - Validates service code format
        - Returns None if service not found or not accessible

        Args:
            tenant_code: Tenant code for isolation
            service_code: Service code identifier

        Returns:
            Dict with service details or None if not found

        Raises:
            ServiceValidationError: If validation fails
        """
        # Business logic: Validate request using domain validators
        ServicesMstValidator.validate_get_service_detail_request(
            tenant_code=tenant_code,
            service_code=service_code
        )

        # Call repository layer
        result = await self.services_repository.get_service_detail(
            tenant_code=tenant_code,
            service_code=service_code
        )

        # Business logic: Additional processing if needed
        # (e.g., enrich with external data, apply business rules)

        return result

    async def update_service_owner(
        self,
        tenant_code: str,
        service_code: str,
        owner_user_code: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Reassign a service's owner. Returns None if the service is not found.
        Raises ValueError if the target user is not in the tenant.
        """
        return await self.services_repository.update_owner(
            tenant_code=tenant_code,
            service_code=service_code,
            owner_user_code=owner_user_code,
        )

    async def delete_service(self, tenant_code: str, service_code: str) -> Dict[str, Any]:
        """
        Soft delete a service by code.
        Sets is_deleted=True and is_active=False. Does not remove the record.
        """
        result = await self.services_repository.soft_delete_by_code(
            code=service_code,
            tenant_code=tenant_code,
        )
        if not result:
            return None
        return {
            'service_code': result.code,
            'service_name': result.name,
            'is_deleted': result.is_deleted,
            'is_active': result.is_active,
            'message': 'Service deleted successfully',
        }

    async def create_service(self, tenant_code: str, user_code: str, data: CreateServiceRequest) -> Dict[str, Any]:
        """
        Create a new service.

        Business Logic:
        - Validates all input fields using domain validators
        - Generates unique service code via factory
        - Creates service record
        - Database constraints ensure referential integrity

        Args:
            tenant_code: Tenant code (from JWT authentication)
            data: CreateServiceRequest with service details

        Returns:
            Dict with created service information

        Raises:
            ServiceValidationError: If validation fails
            ValueError: If application/resource_group doesn't exist (DB constraint)
        """
        # Workspace access guard
        workspace_svc = WorkspaceService(self.session)
        if not await workspace_svc.verify_app_workspace_access(user_code, tenant_code, data.application_code):
            raise ValueError("Access denied: application workspace is not accessible")
        app_svc = ApplicationsMstService(self.session)
        if not await app_svc.can_write_for_app(user_code, tenant_code, data.application_code):
            raise ValueError("Access denied: read-only role cannot create services")

        # Business logic: Validate request using domain validators
        ServicesMstValidator.validate_create_service_request(
            tenant_code=tenant_code,
            application_code=data.application_code,
            resource_group_code=data.resource_group_code,
            service_name=data.service_name,
            service_type=data.service_type
        )

        # Reject duplicate service name within the same tenant + application
        # (case-insensitive, non-deleted only).
        if await self.services_repository.exists_active_by_name(
            tenant_code=tenant_code,
            application_code=data.application_code,
            service_name=data.service_name,
        ):
            raise ServiceValidationError([
                "service_name already exists in this application"
            ])

        # Use factory to create service data dictionary
        service_data = make_service(data, tenant_code)

        # Attribute ownership to the creating user.
        service_data['owner_user_code'] = user_code

        # Create service via repository (base create method)
        # Database foreign key constraints will validate references.
        # The partial unique index guards against a concurrent duplicate that
        # slipped past the check above.
        try:
            created_service = await self.services_repository.create(**service_data)
        except IntegrityError as e:
            if "uq_services_mst_tenant_app_name" in str(e.orig):
                raise ServiceValidationError([
                    "service_name already exists in this application"
                ])
            raise

        # Return response data
        # NOTE: infra_vendor_enum has been moved to service_config
        return {
            'id': created_service.id,
            'service_code': created_service.code,
            'service_name': created_service.name,
            'tenant_code': created_service.tenants_mst_code,
            'application_code': created_service.applications_mst_code,
            'resource_group_code': created_service.resource_group_mst_code,
            'service_type': created_service.service_type,
            'is_active': created_service.is_active,
            'is_public_facing': created_service.is_public_facing,
            'created_at': created_service.created_at,
            'message': 'Service created successfully'
        }
