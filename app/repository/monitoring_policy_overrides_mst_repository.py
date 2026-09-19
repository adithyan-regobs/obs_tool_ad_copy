from typing import List, Optional, Dict, Any
from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import uuid4


from app.db.models.monitoring_policy_overrides_mst_model import MonitoringPolicyOverridesMstModel
from app.repository.base_repository import BaseRepository


class MonitoringPolicyOverridesMstRepository(BaseRepository[MonitoringPolicyOverridesMstModel]):
    """Repository for Monitoring Policy Overrides operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(MonitoringPolicyOverridesMstModel, session)

    async def get_by_infrastructuretype_ref_code(self, infrastructuretype_ref_code: str) -> List[MonitoringPolicyOverridesMstModel]:
        """Get all monitoring policies by infrastructuretype_ref_code"""
        stmt = select(self.model).where(self.model.infrastructuretype_ref_code == infrastructuretype_ref_code)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_override_by_scope(
        self,
        monitoring_policy_defaults_ref_code: str,
        resource_group_mst_code: Optional[str] = None,
        applications_mst_code: Optional[str] = None,
        tenants_mst_code: Optional[str] = None,
        infrastructuretype_ref_code: Optional[str] = None,
        alerttype_ref_code: Optional[str] = None
    ) -> Optional[MonitoringPolicyOverridesMstModel]:
        """
        Get policy override by exact scope match.

        This method implements the 8-level override hierarchy by checking for an exact match
        at a specific scope level. The service layer should call this method multiple times
        in order of decreasing specificity to implement the cascading lookup.

        Specificity Scoring (for reference):
        - Resource Group = 8 points
        - Infrastructure Type = 4 points
        - Application = 2 points
        - Tenant = 1 point

        Args:
            monitoring_policy_defaults_ref_code: Base policy being overridden
            resource_group_mst_code: Resource group scope (most specific)
            applications_mst_code: Application scope
            tenants_mst_code: Tenant scope
            infrastructuretype_ref_code: Infrastructure type filter
            alerttype_ref_code: Alert type filter (optional, for duplicate checking)

        Returns:
            MonitoringPolicyOverridesMstModel if exact match found, None otherwise

        Example:
            # Level 1: Resource Group + InfraType + Tenant (13 points)
            override = await repo.get_override_by_scope(
                monitoring_policy_defaults_ref_code="ec2_cpu_default",
                resource_group_mst_code="rg_001",
                infrastructuretype_ref_code="ec2",
                tenants_mst_code="acme_corp"
            )

            # Duplicate check with alert type
            override = await repo.get_override_by_scope(
                monitoring_policy_defaults_ref_code="ec2_cpu_default",
                infrastructuretype_ref_code="ec2",
                alerttype_ref_code="cpu_util",
                tenants_mst_code="acme_corp"
            )
        """
        conditions = [
            self.model.monitoring_policy_defaults_ref_code == monitoring_policy_defaults_ref_code
        ]

        # Add conditions for each scope parameter
        # NULL checks are important for exact scope matching
        if resource_group_mst_code is not None:
            conditions.append(self.model.resource_group_mst_code == resource_group_mst_code)
        else:
            conditions.append(self.model.resource_group_mst_code.is_(None))

        if applications_mst_code is not None:
            conditions.append(self.model.applications_mst_code == applications_mst_code)
        else:
            conditions.append(self.model.applications_mst_code.is_(None))

        if tenants_mst_code is not None:
            conditions.append(self.model.tenants_mst_code == tenants_mst_code)
        else:
            conditions.append(self.model.tenants_mst_code.is_(None))

        if infrastructuretype_ref_code is not None:
            conditions.append(self.model.infrastructuretype_ref_code == infrastructuretype_ref_code)
        else:
            conditions.append(self.model.infrastructuretype_ref_code.is_(None))

        # Alert type check - only add condition if explicitly provided
        # This allows backward compatibility for existing calls that don't pass alerttype_ref_code
        if alerttype_ref_code is not None:
            conditions.append(self.model.alerttype_ref_code == alerttype_ref_code)

        stmt = select(self.model).where(and_(*conditions))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_overrides_for_tenant(
        self,
        tenant_code: str,
        application_code: Optional[str] = None,
        resource_group_code: Optional[str] = None,
        infrastructuretype_code: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all policy overrides for a specific tenant with optional filters.

        This method retrieves overrides that match the tenant and any additional scope filters.
        It implements the hierarchical override system by finding all applicable overrides
        at various specificity levels.

        Args:
            tenant_code: Tenant code (required for tenant isolation)
            application_code: Optional application scope filter
            resource_group_code: Optional resource group scope filter
            infrastructuretype_code: Optional infrastructure type filter

        Returns:
            List of override policies as dictionaries with specificity scoring
        """
        conditions = [
            self.model.is_deleted == False,
            # Tenant isolation - must match this tenant OR be a global override (NULL tenant)
            or_(
                self.model.tenants_mst_code == tenant_code,
                self.model.tenants_mst_code.is_(None)
            )
        ]

        # Add optional filters - these filter DOWN the results
        # If application_code is provided, only return overrides for that app or global (NULL)
        if application_code:
            conditions.append(
                or_(
                    self.model.applications_mst_code == application_code,
                    self.model.applications_mst_code.is_(None)
                )
            )

        if resource_group_code:
            conditions.append(
                or_(
                    self.model.resource_group_mst_code == resource_group_code,
                    self.model.resource_group_mst_code.is_(None)
                )
            )

        if infrastructuretype_code:
            conditions.append(
                or_(
                    self.model.infrastructuretype_ref_code == infrastructuretype_code,
                    self.model.infrastructuretype_ref_code.is_(None)
                )
            )

        # Query overrides (alerttype_ref_code is now directly in the model)
        stmt = (
            select(self.model)
            .where(and_(*conditions))
            .order_by(
                self.model.applications_mst_code,
                self.model.resource_group_mst_code,
                self.model.infrastructuretype_ref_code
            )
        )

        result = await self.session.execute(stmt)
        overrides = result.scalars().all()

        # Convert to dictionaries with specificity scoring
        overrides_dict = []
        for override in overrides:
            # Calculate specificity score
            specificity_score = 0
            scope_parts = []

            if override.resource_group_mst_code:
                specificity_score += 8
                scope_parts.append(f"Resource Group: {override.resource_group_mst_code}")

            if override.infrastructuretype_ref_code:
                specificity_score += 4
                scope_parts.append(f"Infrastructure Type: {override.infrastructuretype_ref_code}")

            if override.applications_mst_code:
                specificity_score += 2
                scope_parts.append(f"Application: {override.applications_mst_code}")

            if override.tenants_mst_code:
                specificity_score += 1
                scope_parts.append(f"Tenant: {override.tenants_mst_code}")

            override_source = " + ".join(scope_parts) if scope_parts else "Global override"

            overrides_dict.append({
                "code": override.code,
                "name": override.name,
                "description": override.description,
                "monitoring_policy_defaults_ref_code": override.monitoring_policy_defaults_ref_code,
                "tenants_mst_code": override.tenants_mst_code,
                "applications_mst_code": override.applications_mst_code,
                "resource_group_mst_code": override.resource_group_mst_code,
                "infrastructuretype_ref_code": override.infrastructuretype_ref_code,
                "alerttype_ref_code": override.alerttype_ref_code,
                "comparator": override.comparator,
                "threshold_value": override.threshold_value,
                "threshold_unit": override.threshold_unit,
                "eval_window": override.eval_window,
                "for_duration": override.for_duration,
                "no_data": override.no_data,
                "severity": override.severity,
                "is_active": override.is_active,
                "specificity_score": specificity_score,
                "override_source": override_source,
                "created_at": override.created_at,
                "updated_at": override.updated_at
            })

        return overrides_dict

    async def get_by_code(self, code: str) -> Optional[MonitoringPolicyOverridesMstModel]:
        """
        Get policy override by code.

        Args:
            code: Unique override code

        Returns:
            MonitoringPolicyOverridesMstModel or None if not found or deleted
        """
        stmt = select(self.model).where(
            self.model.code == code,
            self.model.is_deleted == False
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_override(
        self,
        override: MonitoringPolicyOverridesMstModel,
        updates: Dict[str, Any]
    ) -> MonitoringPolicyOverridesMstModel:
        """
        Update policy override with provided fields.

        Args:
            override: The override model instance to update
            updates: Dictionary of field names and values to update

        Returns:
            Updated MonitoringPolicyOverridesMstModel

        Note:
            This method only updates the fields provided in the updates dict.
            Immutable fields (code, scope fields) should not be in the updates dict.
        """
        for key, value in updates.items():
            print(key,value)
            setattr(override, key, value)

        await self.session.flush()
        await self.session.refresh(override)
        return override
