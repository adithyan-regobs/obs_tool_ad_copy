from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.factories.resource_group_mst_factory import make_resource_group_mst
from app.repository.resource_group_mst_repository import ResourceGroupMstRepository
from app.repository.applications_mst_repository import ApplicationsMstRepository
from app.services.workspace_service import WorkspaceService


class ResourceGroupMstService:
    """
    Service layer for Resource Group Master operations.
    Handles business logic and orchestrates repository calls.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.resource_groups_repository = ResourceGroupMstRepository(session)
        self.applications_repository = ApplicationsMstRepository(session)

    async def get_all_resource_groups(
        self,
        tenant_code: str,
        user_code: str,
        application_code: Optional[str] = None,
        kind: Optional[str] = None,
        skip: int = 0,
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        Get all resource groups with complete information.

        Business Logic:
        - Enforces tenant isolation
        - Validates pagination parameters
        - Optionally filters by application

        Args:
            tenant_code: Tenant code for isolation (required)
            application_code: Optional application code filter
            kind: Optional 'service' / 'infra' filter. A service belongs in a
                'service' group; 'infra' groups hold S3, SQS, DynamoDB and the
                like, so a caller placing a service should pass 'service'.
            skip: Pagination offset
            limit: Page size

        Returns:
            Dict with total count and list of resource groups

        Raises:
            ValueError: If validation fails
        """
        # Basic validation
        if not tenant_code or not tenant_code.strip():
            raise ValueError("tenant_code is required")

        if skip < 0:
            raise ValueError("skip must be >= 0")

        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")

        workspace_svc = WorkspaceService(self.session)

        if application_code:
            if not await workspace_svc.verify_app_workspace_access(user_code, tenant_code, application_code):
                return {"total": 0, "resource_groups": []}
            accessible_app_codes = None
        else:
            workspace_codes = await workspace_svc.get_user_workspace_codes(user_code, tenant_code)
            accessible_app_codes = await self.applications_repository.get_codes_by_workspaces(
                workspace_codes, tenant_code
            )
            if not accessible_app_codes:
                return {"total": 0, "resource_groups": []}

        # Delegate to repository
        return await self.resource_groups_repository.get_all_resource_groups(
            tenant_code=tenant_code,
            application_code=application_code,
            application_codes=accessible_app_codes,
            kind=kind,
            skip=skip,
            limit=limit
        )

    async def create_resource_group(
        self,
        tenant_code: str,
        name: str,
        application_code: str,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a resource group under one application.

        kind is always "service": these groups hold services, and it is the
        kind the create-service picker lists. Infra grouping uses its own kind
        and is not created from this surface.

        Writes app_db only. The group has no presence in OpenFGA until a tuple
        names it — a role granted in Access Control, or the parent tuple written
        when the first service is assigned to it. Until someone holds a role on
        it, services under it are reachable by org owners alone, which is what
        the service panel means by "Ask an administrator for a role on its
        resource group".
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        if not application_code or not application_code.strip():
            raise ValueError("application_code is required")

        # Tenant isolation: the application must belong to the caller's tenant,
        # or a caller could hang a group off another tenant's application and
        # every role granted on it would reach their services.
        application = await self.applications_repository.get_by_code(application_code)
        if application is None or application.is_deleted:
            raise ValueError("application not found")
        if application.tenants_mst_code != tenant_code:
            raise ValueError("application not found")

        existing = await self.resource_groups_repository.get_all_resource_groups(
            tenant_code=tenant_code, application_code=application_code, limit=500,
        )
        if any(rg["name"].lower() == name.lower() for rg in existing["resource_groups"]):
            # Checked here as well as by uq_app_group_appcode_name so the caller
            # gets a message naming the conflict instead of an integrity error.
            raise ValueError(f"a resource group named '{name}' already exists in this application")

        rg_data = make_resource_group_mst(
            name=name,
            kind="service",
            tenant_code=tenant_code,
            application_code=application_code,
            description=description,
        )
        rg = await self.resource_groups_repository.create(**rg_data)
        return {
            "code": rg.code,
            "name": rg.name,
            "kind": rg.kind,
            "description": rg.description,
            "applications_mst_code": rg.applications_mst_code,
            "tenants_mst_code": rg.tenants_mst_code,
            "is_active": rg.is_active,
            "message": "Resource group created successfully",
        }
