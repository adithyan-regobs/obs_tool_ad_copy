"""
Infrastructure Vendor Accounts repository with AWS assume role support
"""

from typing import Optional, List
from sqlalchemy import select, distinct, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.enum import InfraVendorEnum, EnvironmentEnum

from app.db.models.infra_vendor_accounts_mst_model import InfraVendorAccountsMstModel
from app.repository.base_repository import BaseRepository


class InfraVendorAccountsMstRepository(BaseRepository[InfraVendorAccountsMstModel]):
    """Repository for Infrastructure Vendor Accounts Master operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(InfraVendorAccountsMstModel, session)

    async def get_by_service_hierarchy(
        self,
        service_code: str,
        resource_group_code: Optional[str],
        application_code: Optional[str],
        tenant_code: str,
        environment: str,
        vendor: InfraVendorEnum
    ) -> Optional[InfraVendorAccountsMstModel]:
        """
        Get infrastructure vendor account using hierarchical service lookup.

        Lookup priority (most specific first):
        1. Resource Group-specific account
        2. Application-level account
        3. Tenant-level account (fallback)

        Args:
            service_code: Service code (not used in lookup, for logging only)
            resource_group_code: Resource group code
            application_code: Application code
            tenant_code: Tenant code
            environment: Environment string (dev, staging, prod)
            vendor: Infrastructure vendor enum

        Returns:
            InfraVendorAccountsMstModel if found, None otherwise
        """
        # Convert environment string to enum
        env_enum = EnvironmentEnum(environment)

        # Delegate to get_by_hierarchy
        return await self.get_by_hierarchy(
            infra_vendor_enum=vendor,
            environments_enum=env_enum,
            resource_group_code=resource_group_code,
            application_code=application_code,
            tenant_code=tenant_code
        )

    async def get_by_hierarchy(
        self,
        infra_vendor_enum: InfraVendorEnum,
        environments_enum: EnvironmentEnum,
        resource_group_code: Optional[str] = None,
        application_code: Optional[str] = None,
        tenant_code: Optional[str] = None
    ) -> Optional[InfraVendorAccountsMstModel]:
        """
        Get infrastructure vendor account using hierarchical lookup with tenant isolation.

        Lookup priority with proper isolation:
        1. Resource Group-specific account (within application and tenant)
        2. Application-level account (within tenant)
        3. Tenant-level account (fallback)

        Multi-tenant isolation ensures:
        - Resource groups are only searched within their parent application and tenant
        - Applications are only searched within their parent tenant
        - Each level maintains the hierarchy: Tenant → Application → Resource Group

        Note: All vendor accounts MUST have a tenant. No global fallback exists.
        """
        # Tenant is required for multi-tenant isolation
        if not tenant_code:
            return None

        stmt = select(self.model).where(
            and_(
                self.model.infra_vendor_enum == infra_vendor_enum,
                self.model.environments_enum == environments_enum,
                self.model.is_active == True,
                self.model.is_deleted == False,
                self.model.tenants_mst_code == tenant_code,  # Always filter by tenant
                or_(
                    # Level 1: Resource Group-specific account (within application and tenant)
                    (self.model.resource_group_mst_code == resource_group_code) &
                    (self.model.applications_mst_code == application_code),
                    # Level 2: Application-level account (within tenant)
                    (self.model.resource_group_mst_code.is_(None)) &
                    (self.model.applications_mst_code == application_code),
                    # Level 3: Tenant-level account (fallback)
                    (self.model.resource_group_mst_code.is_(None)) &
                    (self.model.applications_mst_code.is_(None))
                )
            )
        ).order_by(
            # Order by specificity: resource_group > app > tenant
            self.model.resource_group_mst_code.desc().nullslast(),
            self.model.applications_mst_code.desc().nullslast()
        ).limit(1)

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_tenant_and_vendor(
        self,
        tenant_code: str,
        infra_vendor_enum: InfraVendorEnum,
        environments_enum: EnvironmentEnum
    ) -> Optional[InfraVendorAccountsMstModel]:
        """
        Get infrastructure vendor account by tenant, vendor, and environment.
        """
        stmt = select(self.model).where(
            and_(
                self.model.tenants_mst_code == tenant_code,
                self.model.infra_vendor_enum == infra_vendor_enum,
                self.model.environments_enum == environments_enum,
                self.model.is_active == True,
                self.model.is_deleted == False
            )
        ).limit(1)

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_by_tenant(
        self,
        tenant_code: str
    ) -> List[InfraVendorAccountsMstModel]:
        """
        Get all infrastructure vendor accounts for a tenant.

        Args:
            tenant_code: Tenant code

        Returns:
            List of InfraVendorAccountsMstModel
        """
        stmt = select(self.model).where(
            and_(
                self.model.tenants_mst_code == tenant_code,
                self.model.is_active == True,
                self.model.is_deleted == False
            )
        )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    EXCLUDED_VENDORS_FROM_HIERARCHY = {}

    async def get_distinct_vendors_by_hierarchy(
        self,
        tenant_code: str,
        environments_enum: EnvironmentEnum,
        application_code: Optional[str] = None,
        resource_group_code: Optional[str] = None,
    ) -> List[str]:
        """
        Get distinct vendor enums available using bottom-up hierarchical lookup.

        Lookup priority (most specific first):
        1. Resource group level (resource_group + application + tenant + env)
        2. Application level (application + tenant + env)
        3. Tenant level (all vendors for tenant + env, regardless of app/rg)

        Returns list of vendor enum values (e.g. ["aws", "gcp"])
        """
        excluded = self.EXCLUDED_VENDORS_FROM_HIERARCHY

        # Level 1: resource-group-specific vendors
        if resource_group_code and application_code:
            stmt = select(distinct(self.model.infra_vendor_enum)).where(
                and_(
                    self.model.tenants_mst_code == tenant_code,
                    self.model.environments_enum == environments_enum,
                    self.model.applications_mst_code == application_code,
                    self.model.resource_group_mst_code == resource_group_code,
                    self.model.infra_vendor_enum.notin_(excluded),
                    self.model.is_active == True,
                    self.model.is_deleted == False,
                )
            )
            result = await self.session.execute(stmt)
            vendors = [row[0].value for row in result.fetchall()]
            if vendors:
                return vendors

        # Level 2: application-specific vendors
        if application_code:
            stmt = select(distinct(self.model.infra_vendor_enum)).where(
                and_(
                    self.model.tenants_mst_code == tenant_code,
                    self.model.environments_enum == environments_enum,
                    self.model.applications_mst_code == application_code,
                    self.model.infra_vendor_enum.notin_(excluded),
                    self.model.is_active == True,
                    self.model.is_deleted == False,
                )
            )
            result = await self.session.execute(stmt)
            vendors = [row[0].value for row in result.fetchall()]
            if vendors:
                return vendors

        # Level 3: tenant-level fallback — all vendors for this tenant+env
        stmt = select(distinct(self.model.infra_vendor_enum)).where(
            and_(
                self.model.tenants_mst_code == tenant_code,
                self.model.environments_enum == environments_enum,
                self.model.infra_vendor_enum.notin_(excluded),
                self.model.is_active == True,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return [row[0].value for row in result.fetchall()]
