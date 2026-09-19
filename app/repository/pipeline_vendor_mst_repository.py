"""
Pipeline Vendor Master Repository
Handles data access operations for pipeline_vendor_mst table
"""
from typing import Optional
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.pipeline_vendor_mst_model import PipelineVendorMstModel
from app.repository.base_repository import BaseRepository


class PipelineVendorMstRepository(BaseRepository[PipelineVendorMstModel]):
    """Repository for PipelineVendorMst operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(PipelineVendorMstModel, session)

    async def get_by_service_hierarchy(
        self,
        service_code: str,
        resource_group_code: str,
        application_code: str,
        tenant_code: str,
        environment: str
    ) -> Optional[PipelineVendorMstModel]:
        """
        Get pipeline vendor configuration using hierarchical lookup with tenant isolation.

        Lookup priority with proper isolation:
        1. Service-level (within resource group, application, and tenant)
        2. Resource group-level (within application and tenant)
        3. Application-level (within tenant)
        4. Tenant-level (fallback)

        Multi-tenant isolation ensures:
        - Services are only searched within their parent resource group, application, and tenant
        - Resource groups are only searched within their parent application and tenant
        - Applications are only searched within their parent tenant
        - Each level maintains the hierarchy: Tenant → Application → Resource Group → Service

        Args:
            service_code: Service code from services_mst
            resource_group_code: Resource group code
            application_code: Application code
            tenant_code: Tenant code
            environment: Deployment environment (dev, staging, prod)

        Returns:
            Pipeline vendor configuration if found, None otherwise
        """
        # Level 1: Service-level (within resource group, application, and tenant)
        service_level = await self._get_by_service_and_env(
            service_code, resource_group_code, application_code, tenant_code, environment
        )
        if service_level:
            return service_level

        # Level 2: Resource group-level (within application and tenant)
        resource_group_level = await self._get_by_resource_group_and_env(
            resource_group_code, application_code, tenant_code, environment
        )
        if resource_group_level:
            return resource_group_level

        # Level 3: Application-level (within tenant)
        application_level = await self._get_by_application_and_env(
            application_code, tenant_code, environment
        )
        if application_level:
            return application_level

        # Level 4: Tenant-level (fallback)
        tenant_level = await self._get_by_tenant_and_env(tenant_code, environment)
        if tenant_level:
            return tenant_level

        # No configuration found at any level
        return None

    async def _get_by_service_and_env(
        self,
        service_code: str,
        resource_group_code: str,
        application_code: str,
        tenant_code: str,
        environment: str
    ) -> Optional[PipelineVendorMstModel]:
        """Get pipeline vendor by service code (within resource group, application, and tenant)"""
        stmt = select(self.model).where(
            and_(
                self.model.service_mst_code == service_code,
                self.model.resource_group_mst_code == resource_group_code,
                self.model.applications_mst_code == application_code,
                self.model.tenants_mst_code == tenant_code,
                self.model.environment == environment
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_by_resource_group_and_env(
        self,
        resource_group_code: str,
        application_code: str,
        tenant_code: str,
        environment: str
    ) -> Optional[PipelineVendorMstModel]:
        """Get pipeline vendor by resource group (within application and tenant)"""
        stmt = select(self.model).where(
            and_(
                self.model.resource_group_mst_code == resource_group_code,
                self.model.applications_mst_code == application_code,
                self.model.tenants_mst_code == tenant_code,
                self.model.environment == environment,
                # Service code should be null for resource group level config
                self.model.service_mst_code.is_(None)
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_by_application_and_env(
        self,
        application_code: str,
        tenant_code: str,
        environment: str
    ) -> Optional[PipelineVendorMstModel]:
        """Get pipeline vendor by application (within tenant)"""
        stmt = select(self.model).where(
            and_(
                self.model.applications_mst_code == application_code,
                self.model.tenants_mst_code == tenant_code,
                self.model.environment == environment,
                # Service and resource group should be null for application level
                self.model.service_mst_code.is_(None),
                self.model.resource_group_mst_code.is_(None)
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_by_tenant_and_env(
        self,
        tenant_code: str,
        environment: str
    ) -> Optional[PipelineVendorMstModel]:
        """Get pipeline vendor by tenant (fallback)"""
        stmt = select(self.model).where(
            and_(
                self.model.tenants_mst_code == tenant_code,
                self.model.environment == environment,
                # All other codes should be null for tenant level
                self.model.service_mst_code.is_(None),
                self.model.resource_group_mst_code.is_(None),
                self.model.applications_mst_code.is_(None)
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Optional[PipelineVendorMstModel]:
        """
        Get pipeline vendor by code.

        Args:
            code: Pipeline vendor code

        Returns:
            Pipeline vendor if found, None otherwise
        """
        return await self.get_by(code=code)
