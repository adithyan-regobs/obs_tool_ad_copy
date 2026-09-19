from typing import Optional, Dict, Any, List
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.resource_group_mst_model import ResourceGroupMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.services_mst_model import ServicesMstModel
from app.repository.base_repository import BaseRepository


class ResourceGroupMstRepository(BaseRepository[ResourceGroupMstModel]):
    """Repository for Resource Group Master operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ResourceGroupMstModel, session)

    async def get_by_code(self, code: str) -> Optional[ResourceGroupMstModel]:
        """Get resource group by code"""
        stmt = select(self.model).where(self.model.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_resource_groups(
        self,
        tenant_code: str,
        application_code: Optional[str] = None,
        application_codes: Optional[List[str]] = None,
        kind: Optional[str] = None,
        skip: int = 0,
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        Get all resource groups with complete information.

        Args:
            tenant_code: Tenant code (required for tenant isolation)
            application_code: Optional filter by application code
            kind: Optional filter by kind ('service' or 'infra'); omit for both
            skip: Pagination offset (default: 0)
            limit: Page size (default: 100, max: 500)

        Returns:
            Dictionary with:
            - total: Total number of matching resource groups
            - resource_groups: List of resource group dictionaries

        Security:
            Enforces tenant isolation - only returns resource groups for specified tenant
        """
        # Live services per group, as a correlated scalar subquery rather than a
        # join + GROUP BY: a join would multiply the group rows before the
        # LIMIT/OFFSET below and break pagination.
        services_count = (
            select(func.count(ServicesMstModel.id))
            .where(
                ServicesMstModel.resource_group_mst_code == ResourceGroupMstModel.code,
                ServicesMstModel.is_deleted == False,  # noqa: E712
            )
            .correlate(ResourceGroupMstModel)
            .scalar_subquery()
            .label('services_count')
        )

        # Main query with JOINs
        stmt = (
            select(
                ResourceGroupMstModel.id,
                ResourceGroupMstModel.code.label('resource_group_code'),
                ResourceGroupMstModel.name.label('resource_group_name'),
                ResourceGroupMstModel.kind,
                ResourceGroupMstModel.description,
                ResourceGroupMstModel.is_active,
                ResourceGroupMstModel.created_at,
                ResourceGroupMstModel.updated_at,
                ResourceGroupMstModel.applications_mst_code,
                ApplicationsMstModel.name.label('application_name'),
                ResourceGroupMstModel.tenants_mst_code,
                services_count,
            )
            .select_from(ResourceGroupMstModel)
            .join(
                ApplicationsMstModel,
                ResourceGroupMstModel.applications_mst_code == ApplicationsMstModel.code
            )
            .where(ResourceGroupMstModel.is_deleted == False)
            .where(ResourceGroupMstModel.tenants_mst_code == tenant_code)  # Tenant isolation
        )

        # Apply optional application filter
        if application_code:
            stmt = stmt.where(ResourceGroupMstModel.applications_mst_code == application_code)
        elif application_codes is not None:
            stmt = stmt.where(ResourceGroupMstModel.applications_mst_code.in_(application_codes))

        # Apply optional kind filter ('service' / 'infra'). A caller placing a
        # service passes 'service' so infra groups (S3, SQS, ...) stay out of
        # the list; omitting it returns both, as before.
        if kind:
            stmt = stmt.where(ResourceGroupMstModel.kind == kind)

        # Get total count before pagination
        count_stmt = select(func.count()).select_from(stmt.subquery())
        count_result = await self.session.execute(count_stmt)
        total = count_result.scalar() or 0

        # Apply ordering and pagination
        stmt = stmt.order_by(ResourceGroupMstModel.created_at.desc())
        stmt = stmt.offset(skip).limit(limit)

        result = await self.session.execute(stmt)
        resource_groups_data = result.all()

        # Build response
        resource_groups = []
        for row in resource_groups_data:
            resource_groups.append({
                'id': row.id,
                'code': row.resource_group_code,
                'name': row.resource_group_name,
                'kind': row.kind,
                'description': row.description,
                'applications_mst_code': row.applications_mst_code,
                'application_name': row.application_name,
                'services_count': row.services_count,
                'tenants_mst_code': row.tenants_mst_code,
                'is_active': row.is_active,
                'created_at': row.created_at,
                'updated_at': row.updated_at,
            })

        return {
            'total': total,
            'resource_groups': resource_groups
        }

    async def get_or_create_default_infra_group(
        self,
        tenant_code: str,
        application_code: str
    ) -> ResourceGroupMstModel:
        """
        Get or create a default infrastructure resource group for a tenant/application.

        This is used when infrastructure resources (S3, EC2, etc.) are created without
        an explicit resource group specified.

        Args:
            tenant_code: Tenant code
            application_code: Application code

        Returns:
            ResourceGroupMstModel (existing or newly created)

        Example:
            rg = await repo.get_or_create_default_infra_group(
                tenant_code="vance",
                application_code="core"
            )
        """
        from app.domain.factories.resource_group_mst_factory import make_resource_group_mst

        # Try to find existing default infra resource group
        stmt = select(self.model).where(
            self.model.tenants_mst_code == tenant_code,
            self.model.applications_mst_code == application_code,
            self.model.kind == 'infra',
            self.model.name == 'default-infra',
            self.model.is_deleted == False
        )
        result = await self.session.execute(stmt)
        existing_rg = result.scalar_one_or_none()

        if existing_rg:
            return existing_rg

        # Create new default infra resource group
        rg_data = make_resource_group_mst(
            name="default-infra",
            kind="infra",
            tenant_code=tenant_code,
            application_code=application_code
        )

        new_rg = self.model(**rg_data)
        self.session.add(new_rg)
        await self.session.flush()  # Flush to get the ID, but don't commit yet

        return new_rg

    async def get_first_by_application(
        self,
        tenant_code: str,
        application_code: str,
    ) -> Optional[ResourceGroupMstModel]:
        """Return the first active resource group for the given tenant + application.

        Used by the DevLift MCP `provision_service` tool to auto-resolve the
        resource_group_code without asking the user.
        """
        stmt = (
            select(ResourceGroupMstModel)
            .where(
                ResourceGroupMstModel.tenants_mst_code == tenant_code,
                ResourceGroupMstModel.applications_mst_code == application_code,
                ResourceGroupMstModel.is_deleted == False,  # noqa: E712
            )
            .order_by(ResourceGroupMstModel.created_at.asc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
