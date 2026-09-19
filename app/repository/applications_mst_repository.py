from typing import Optional, Dict, Any, List
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.resource_group_mst_model import ResourceGroupMstModel
from app.db.models.alert_config_model import AlertConfigModel
from app.db.models.service_dependency_map_model import ServiceDependencyMapModel
from app.repository.base_repository import BaseRepository


class ApplicationsMstRepository(BaseRepository[ApplicationsMstModel]):
    """Repository for Applications Master operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ApplicationsMstModel, session)

    async def get_codes_by_workspaces(
        self,
        workspace_codes: List[str],
        tenant_code: str,
    ) -> List[str]:
        """
        Returns application codes that belong to any of the given workspaces
        within the tenant.
        """
        if not workspace_codes:
            return []
        stmt = select(ApplicationsMstModel.code).where(
            ApplicationsMstModel.workspace_code.in_(workspace_codes),
            ApplicationsMstModel.tenants_mst_code == tenant_code,
            ApplicationsMstModel.is_deleted == False,
        )
        result = await self.session.execute(stmt)
        return [row[0] for row in result.all()]

    async def get_by_code(self, code: str) -> Optional[ApplicationsMstModel]:
        """Get application by code"""
        stmt = select(self.model).where(self.model.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_applications(
        self,
        tenant_code: str,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: int = 100,
        workspace_code: Optional[str] = None,
        workspace_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Get all applications with complete information including tenant,
        services count, resource groups count, alerts count, and infrastructure count.

        Args:
            tenant_code: Tenant code (required for tenant isolation)
            is_active: Filter by active status - True/False/None (optional)
            skip: Pagination offset (default: 0)
            limit: Page size (default: 100, max: 500)

        Returns:
            Dictionary with:
            - total: Total number of matching applications
            - applications: List of application dictionaries with all details

        Security:
            Enforces tenant isolation - only returns applications for specified tenant
        """
        # Main query with JOINs and aggregations
        stmt = (
            select(
                ApplicationsMstModel.id,
                ApplicationsMstModel.code.label('application_code'),
                ApplicationsMstModel.name.label('application_name'),
                ApplicationsMstModel.description,
                ApplicationsMstModel.is_active,
                ApplicationsMstModel.created_at,
                ApplicationsMstModel.updated_at,
                ApplicationsMstModel.workspace_code,
                TenantsMstModel.code.label('tenant_code'),
                TenantsMstModel.name.label('tenant_name'),
                func.count(func.distinct(ServicesMstModel.id)).label('services_count'),
                func.count(func.distinct(ResourceGroupMstModel.id)).label('resource_groups_count'),
                func.count(func.distinct(AlertConfigModel.id)).label('alerts_configured'),
            )
            .select_from(ApplicationsMstModel)
            .join(
                TenantsMstModel,
                ApplicationsMstModel.tenants_mst_code == TenantsMstModel.code
            )
            .outerjoin(
                ServicesMstModel,
                ApplicationsMstModel.code == ServicesMstModel.applications_mst_code
            )
            .outerjoin(
                ResourceGroupMstModel,
                ApplicationsMstModel.code == ResourceGroupMstModel.applications_mst_code
            )
            .outerjoin(
                AlertConfigModel,
                ServicesMstModel.code == AlertConfigModel.services_mst_code
            )
            .where(ApplicationsMstModel.is_deleted == False)
            .where(ApplicationsMstModel.tenants_mst_code == tenant_code)  # Mandatory tenant isolation
        )

        # Apply optional filters
        if is_active is not None:
            stmt = stmt.where(ApplicationsMstModel.is_active == is_active)

        if workspace_code is not None:
            stmt = stmt.where(ApplicationsMstModel.workspace_code == workspace_code)
        elif workspace_codes is not None:
            stmt = stmt.where(ApplicationsMstModel.workspace_code.in_(workspace_codes))

        # Group by to handle COUNT aggregation
        stmt = stmt.group_by(
            ApplicationsMstModel.id,
            ApplicationsMstModel.code,
            ApplicationsMstModel.name,
            ApplicationsMstModel.description,
            ApplicationsMstModel.is_active,
            ApplicationsMstModel.created_at,
            ApplicationsMstModel.updated_at,
            ApplicationsMstModel.workspace_code,
            TenantsMstModel.code,
            TenantsMstModel.name,
        )

        # Get total count before pagination
        count_stmt = select(func.count()).select_from(stmt.subquery())
        count_result = await self.session.execute(count_stmt)
        total = count_result.scalar() or 0

        # Apply ordering and pagination
        stmt = stmt.order_by(ApplicationsMstModel.created_at.asc())
        stmt = stmt.offset(skip).limit(limit)

        result = await self.session.execute(stmt)
        applications_data = result.all()

        # Get additional counts for each application
        applications = []
        for row in applications_data:
            # Get infrastructure count from service dependencies
            infra_stmt = select(func.count(func.distinct(ServiceDependencyMapModel.infrastructure_mst_code))).where(
                ServiceDependencyMapModel.applications_mst_code == row.application_code
            )
            infra_result = await self.session.execute(infra_stmt)
            infrastructure_count = infra_result.scalar() or 0

            # Calculate total alerts (configured + available alerts for all services)
            # For now, using configured count as total (can be enhanced to include available alerts)
            alerts_total = row.alerts_configured

            applications.append({
                'id': row.id,
                'application_code': row.application_code,
                'application_name': row.application_name,
                'description': row.description,
                'tenant_code': row.tenant_code,
                'tenant_name': row.tenant_name,
                'workspace_code': row.workspace_code,
                'status': 'active' if row.is_active else 'inactive',
                'is_active': row.is_active,
                'services_count': row.services_count,
                'resource_groups_count': row.resource_groups_count,
                'alerts_configured': row.alerts_configured,
                'alerts_total': alerts_total,
                'infrastructure_count': infrastructure_count,
                'created_at': row.created_at,
                'updated_at': row.updated_at,
            })

        return {
            'total': total,
            'applications': applications
        }
