from typing import Optional, Dict, Any, List
from sqlalchemy import or_, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
import uuid

from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.resource_group_mst_model import ResourceGroupMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.alert_config_model import AlertConfigModel
from app.db.models.service_dependency_map_model import ServiceDependencyMapModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.db.models.user_mst_model import UserMstModel
from app.repository.base_repository import BaseRepository
from app.core.enum import ServiceTypeEnum


class ServicesMstRepository(BaseRepository[ServicesMstModel]):
    """Repository for Services Master operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ServicesMstModel, session)

    async def get_by_code(self, code: str) -> Optional[ServicesMstModel]:
        """Get service by code with tenant, application, and infrastructure relationships loaded"""
        stmt = (
            select(self.model)
            .options(
                joinedload(self.model.tenant),
                joinedload(self.model.application),
                joinedload(self.model.infrastructure)
                .joinedload(InfrastructureMstModel.infra_vendor_account)
            )
            .where(self.model.code == code)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def soft_delete_by_code(self, code: str, tenant_code: str) -> Optional[ServicesMstModel]:
        """Soft delete a service by code within a tenant."""
        stmt = (
            select(self.model)
            .where(self.model.code == code)
            .where(self.model.tenants_mst_code == tenant_code)
            .where(self.model.is_deleted == False)
        )
        result = await self.session.execute(stmt)
        db_obj = result.scalar_one_or_none()
        if not db_obj:
            return None
        db_obj.is_deleted = True
        db_obj.is_active = False
        self.session.add(db_obj)
        await self.session.flush()
        await self.session.refresh(db_obj)
        return db_obj

    async def get_by_application_code(self, application_code: str) -> list[ServicesMstModel]:
        """Get all services for an application"""
        stmt = select(self.model).where(self.model.applications_mst_code == application_code)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_service_detail(
        self,
        tenant_code: str,
        service_code: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get single service with complete details and tenant isolation.
        Optimized: Single query with scalar subqueries instead of 5 separate queries.

        Args:
            tenant_code: Tenant code (for tenant isolation)
            service_code: Service code (auto-generated identifier)

        Returns:
            Dict with service details or None if not found

        Security:
            Enforces tenant isolation - service must belong to specified tenant
        """
        # Scalar subqueries for counts (executed as part of main query)
        alerts_total_subq = (
            select(func.count())
            .where(AlertConfigModel.services_mst_code == ServicesMstModel.code)
            .correlate(ServicesMstModel)
            .scalar_subquery()
        )

        alerts_failed_subq = (
            select(func.count())
            .where(
                AlertConfigModel.services_mst_code == ServicesMstModel.code,
                AlertConfigModel.vendor_status == 'FAILED'
            )
            .correlate(ServicesMstModel)
            .scalar_subquery()
        )

        dependency_count_subq = (
            select(func.count())
            .where(ServiceDependencyMapModel.services_mst_code == ServicesMstModel.code)
            .correlate(ServicesMstModel)
            .scalar_subquery()
        )

        infrastructure_count_subq = (
            select(func.count(func.distinct(ServiceDependencyMapModel.infrastructure_mst_code)))
            .where(ServiceDependencyMapModel.services_mst_code == ServicesMstModel.code)
            .correlate(ServicesMstModel)
            .scalar_subquery()
        )

        # Single query with all data including counts
        stmt = (
            select(
                ServicesMstModel.id,
                ServicesMstModel.code.label('service_code'),
                ServicesMstModel.name.label('service_name'),
                ServicesMstModel.description,
                ServicesMstModel.is_active,
                ServicesMstModel.is_public_facing,
                ServicesMstModel.service_type,
                ServicesMstModel.created_at,
                ServicesMstModel.updated_at,
                TenantsMstModel.code.label('tenant_code'),
                TenantsMstModel.name.label('tenant_name'),
                ApplicationsMstModel.code.label('application_code'),
                ApplicationsMstModel.name.label('application_name'),
                ResourceGroupMstModel.code.label('resource_group_code'),
                ResourceGroupMstModel.name.label('resource_group_name'),
                ResourceGroupMstModel.kind.label('resource_group_kind'),
                alerts_total_subq.label('alerts_total'),
                alerts_failed_subq.label('alerts_failed'),
                dependency_count_subq.label('dependency_count'),
                infrastructure_count_subq.label('infrastructure_count'),
                UserMstModel.first_name.label('owner_first_name'),
                UserMstModel.last_name.label('owner_last_name'),
                UserMstModel.email_id.label('owner_email'),
            )
            .select_from(ServicesMstModel)
            .join(
                ResourceGroupMstModel,
                ServicesMstModel.resource_group_mst_code == ResourceGroupMstModel.code
            )
            .join(
                ApplicationsMstModel,
                ServicesMstModel.applications_mst_code == ApplicationsMstModel.code
            )
            .join(
                TenantsMstModel,
                ServicesMstModel.tenants_mst_code == TenantsMstModel.code
            )
            .outerjoin(
                UserMstModel,
                ServicesMstModel.owner_user_code == UserMstModel.code
            )
            .where(ServicesMstModel.is_deleted == False)
            .where(ServicesMstModel.tenants_mst_code == tenant_code)
            .where(ServicesMstModel.code == service_code)
        )

        result = await self.session.execute(stmt)
        row = result.one_or_none()

        if not row:
            return None

        return {
            'id': row.id,
            'service_code': row.service_code,
            'service_name': row.service_name,
            'description': row.description,
            'tenant_code': row.tenant_code,
            'tenant_name': row.tenant_name,
            'application_code': row.application_code,
            'application_name': row.application_name,
            'resource_group_code': row.resource_group_code,
            'resource_group_name': row.resource_group_name,
            'resource_group_kind': row.resource_group_kind,
            'status': 'active' if row.is_active else 'inactive',
            'is_active': row.is_active,
            'is_public_facing': row.is_public_facing,
            'service_type': row.service_type.value if row.service_type else None,
            'alerts_configured': row.alerts_total,  # Using total as configured
            'alerts_total': row.alerts_total or 0,
            'alerts_failed': row.alerts_failed or 0,
            'infrastructure_count': row.infrastructure_count or 0,
            'dependency_count': row.dependency_count or 0,
            'created_at': row.created_at,
            'updated_at': row.updated_at,
            'owner_name': (
                f"{row.owner_first_name} {row.owner_last_name}".strip()
                if row.owner_first_name or row.owner_last_name
                else None
            ),
            'owner_email': row.owner_email,
            'owner_contact': None,  # user_mst has no contact/phone column yet
        }

    async def update_owner(
        self,
        tenant_code: str,
        service_code: str,
        owner_user_code: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Reassign a service's owner (tenant-isolated).

        Returns None if the service is not found in the tenant.
        Raises ValueError if the target owner user is not in the tenant.
        """
        service = (
            await self.session.execute(
                select(ServicesMstModel).where(
                    ServicesMstModel.code == service_code,
                    ServicesMstModel.tenants_mst_code == tenant_code,
                    ServicesMstModel.is_deleted == False,
                )
            )
        ).scalar_one_or_none()

        if not service:
            return None

        owner = None
        if owner_user_code:
            owner = (
                await self.session.execute(
                    select(UserMstModel).where(
                        UserMstModel.code == owner_user_code,
                        UserMstModel.tenants_mst_code == tenant_code,
                    )
                )
            ).scalar_one_or_none()
            if not owner:
                raise ValueError(
                    f"User '{owner_user_code}' not found in tenant '{tenant_code}'"
                )

        service.owner_user_code = owner_user_code
        await self.session.flush()

        owner_name = (
            f"{owner.first_name} {owner.last_name}".strip() if owner else None
        )
        return {
            'service_code': service.code,
            'owner_user_code': owner_user_code,
            'owner_name': owner_name,
            'owner_email': owner.email_id if owner else None,
        }

    async def get_all_services(
        self,
        tenant_code: str,
        application_code: Optional[str] = None,
        application_codes: Optional[List[str]] = None,
        resource_group_mst_code: Optional[str] = None,
        is_active: Optional[bool] = None,
        search_query: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        allowed_service_codes: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Get all services with complete information including tenant, application,
        resource group, alert counts, and dependencies.
        Optimized: Uses scalar subqueries instead of N+1 queries per service.

        Args:
            tenant_code: Tenant code (required for tenant isolation)
            application_code: Filter by application code (optional)
            is_active: Filter by active status - True/False/None (optional)
            skip: Pagination offset (default: 0)
            limit: Page size (default: 100, max: 500)
            allowed_service_codes: List of service codes user has permission for (optional)

        Returns:
            Dictionary with:
            - total: Total number of matching services
            - services: List of service details with relationships
        """
        # Scalar subqueries for counts (executed as part of main query)
        alerts_total_subq = (
            select(func.count())
            .where(AlertConfigModel.services_mst_code == ServicesMstModel.code)
            .correlate(ServicesMstModel)
            .scalar_subquery()
        )

        alerts_failed_subq = (
            select(func.count())
            .where(
                AlertConfigModel.services_mst_code == ServicesMstModel.code,
                AlertConfigModel.vendor_status == 'FAILED'
            )
            .correlate(ServicesMstModel)
            .scalar_subquery()
        )

        dependency_count_subq = (
            select(func.count())
            .where(ServiceDependencyMapModel.services_mst_code == ServicesMstModel.code)
            .correlate(ServicesMstModel)
            .scalar_subquery()
        )

        infrastructure_count_subq = (
            select(func.count(func.distinct(ServiceDependencyMapModel.infrastructure_mst_code)))
            .where(ServiceDependencyMapModel.services_mst_code == ServicesMstModel.code)
            .correlate(ServicesMstModel)
            .scalar_subquery()
        )

        # Build query with all counts included
        stmt = (
            select(
                ServicesMstModel.id,
                ServicesMstModel.code.label('service_code'),
                ServicesMstModel.name.label('service_name'),
                ServicesMstModel.description,
                ServicesMstModel.is_active,
                ServicesMstModel.is_public_facing,
                ServicesMstModel.service_type,
                ServicesMstModel.created_at,
                ServicesMstModel.updated_at,
                TenantsMstModel.code.label('tenant_code'),
                TenantsMstModel.name.label('tenant_name'),
                ApplicationsMstModel.code.label('application_code'),
                ApplicationsMstModel.name.label('application_name'),
                ResourceGroupMstModel.code.label('resource_group_code'),
                ResourceGroupMstModel.name.label('resource_group_name'),
                ResourceGroupMstModel.kind.label('resource_group_kind'),
                alerts_total_subq.label('alerts_total'),
                alerts_failed_subq.label('alerts_failed'),
                dependency_count_subq.label('dependency_count'),
                infrastructure_count_subq.label('infrastructure_count'),
            )
            .select_from(ServicesMstModel)
            .join(
                ResourceGroupMstModel,
                ServicesMstModel.resource_group_mst_code == ResourceGroupMstModel.code
            )
            .join(
                ApplicationsMstModel,
                ServicesMstModel.applications_mst_code == ApplicationsMstModel.code
            )
            .join(
                TenantsMstModel,
                ServicesMstModel.tenants_mst_code == TenantsMstModel.code
            )
            .where(ServicesMstModel.is_deleted == False)
            .where(ServicesMstModel.tenants_mst_code == tenant_code)
        )

        # Apply optional filters
        if application_code:
            stmt = stmt.where(ServicesMstModel.applications_mst_code == application_code)
        elif application_codes is not None:
            stmt = stmt.where(ServicesMstModel.applications_mst_code.in_(application_codes))
        if resource_group_mst_code:
            stmt = stmt.where(ServicesMstModel.resource_group_mst_code == resource_group_mst_code)
        if is_active is not None:
            stmt = stmt.where(ServicesMstModel.is_active == is_active)
        if search_query and search_query.strip():
            stmt = stmt.where(ServicesMstModel.name.ilike(f"%{search_query.strip()}%"))

        # Permission filter: only show services user has access to
        if allowed_service_codes is not None:
            if len(allowed_service_codes) == 0:
                return {"total": 0, "services": []}
            stmt = stmt.where(ServicesMstModel.code.in_(allowed_service_codes))

        # Count total results before pagination
        count_stmt = select(func.count()).select_from(
            select(ServicesMstModel.id)
            .select_from(ServicesMstModel)
            .join(ResourceGroupMstModel, ServicesMstModel.resource_group_mst_code == ResourceGroupMstModel.code)
            .join(ApplicationsMstModel, ServicesMstModel.applications_mst_code == ApplicationsMstModel.code)
            .join(TenantsMstModel, ServicesMstModel.tenants_mst_code == TenantsMstModel.code)
            .where(ServicesMstModel.is_deleted == False)
            .where(ServicesMstModel.tenants_mst_code == tenant_code)
            .where(ServicesMstModel.applications_mst_code == application_code if application_code else True)
            .where(ServicesMstModel.resource_group_mst_code == resource_group_mst_code if resource_group_mst_code else True)
            .where(ServicesMstModel.is_active == is_active if is_active is not None else True)
            .where(ServicesMstModel.name.ilike(f"%{search_query.strip()}%") if search_query and search_query.strip() else True)
            .where(ServicesMstModel.code.in_(allowed_service_codes) if allowed_service_codes else True)
            .subquery()
        )
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar() or 0

        # Apply pagination and ordering
        stmt = stmt.order_by(ServicesMstModel.created_at.desc()).offset(skip).limit(limit)

        # Execute single query with all data
        result = await self.session.execute(stmt)
        services_data = result.all()

        # Build response list (no additional queries needed)
        services = []
        for row in services_data:
            services.append({
                'id': row.id,
                'service_code': row.service_code,
                'service_name': row.service_name,
                'description': row.description,
                'tenant_code': row.tenant_code,
                'tenant_name': row.tenant_name,
                'application_code': row.application_code,
                'application_name': row.application_name,
                'resource_group_code': row.resource_group_code,
                'resource_group_name': row.resource_group_name,
                'resource_group_kind': row.resource_group_kind,
                'status': 'active' if row.is_active else 'inactive',
                'is_public_facing': row.is_public_facing,
                'service_type': row.service_type.value if row.service_type else None,
                'alerts_configured': row.alerts_total or 0,
                'alerts_total': row.alerts_total or 0,
                'alerts_failed': row.alerts_failed or 0,
                'infrastructure_count': row.infrastructure_count or 0,
                'dependency_count': row.dependency_count or 0,
                'created_at': row.created_at,
                'updated_at': row.updated_at
            })

        return {
            'total': total,
            'services': services
        }

    async def find_by_name_for_mcp(
        self,
        tenant_code: str,
        service_name: str,
    ) -> Optional[ServicesMstModel]:
        """Look up a service by name for the DevLift MCP `provision_service` tool.

        Tries two name variants (case-insensitive exact match):
          1. `{service_name}`           — e.g. "payments"
          2. `{service_name}-service`   — e.g. "payments-service"

        Returns the first active, non-deleted match scoped to the tenant, or None.
        """
        name_lower = service_name.strip().lower()
        variant = f"{name_lower}-service"

        stmt = (
            select(ServicesMstModel)
            .where(
                ServicesMstModel.tenants_mst_code == tenant_code,
                ServicesMstModel.is_deleted == False,   # noqa: E712
                ServicesMstModel.is_active == True,     # noqa: E712
                or_(
                    ServicesMstModel.name.ilike(name_lower),
                    ServicesMstModel.name.ilike(variant),
                ),
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def search_by_name(
        self,
        tenant_code: str,
        name: str,
        limit: int = 8,
    ) -> List[ServicesMstModel]:
        """Services whose name contains `name`, exact matches first.

        For the CLI assistant: "edit demo" must find `demo` (or `demo-service`)
        ahead of `demo-worker`, and a miss must still return near matches so
        the reply can suggest them. Tenant-scoped, active, non-deleted.
        """
        needle = name.strip().lower()
        if not needle:
            return []
        lowered = func.lower(ServicesMstModel.name)
        stmt = (
            select(ServicesMstModel)
            .where(
                ServicesMstModel.tenants_mst_code == tenant_code,
                ServicesMstModel.is_deleted == False,  # noqa: E712
                ServicesMstModel.is_active == True,  # noqa: E712
                ServicesMstModel.name.ilike(f"%{needle}%"),
            )
            .order_by(
                # exact name, then the "-service" variant, then the rest by name
                (lowered != needle),
                (lowered != f"{needle}-service"),
                ServicesMstModel.name,
            )
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def exists_active_by_name(
        self,
        tenant_code: str,
        application_code: str,
        service_name: str,
    ) -> bool:
        """Return True if a non-deleted service with this name (case-insensitive)
        already exists in the given tenant + application."""
        stmt = (
            select(ServicesMstModel.id)
            .where(
                ServicesMstModel.tenants_mst_code == tenant_code,
                ServicesMstModel.applications_mst_code == application_code,
                ServicesMstModel.is_deleted == False,  # noqa: E712
                func.lower(ServicesMstModel.name) == service_name.strip().lower(),
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None
