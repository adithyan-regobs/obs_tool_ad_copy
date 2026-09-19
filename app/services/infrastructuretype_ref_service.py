from typing import Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, distinct

from app.repository.infrastructuretype_ref_repository import InfrastructureTypeRefRepository
from app.db.models.monitoring_policy_defaults_ref_model import MonitoringPolicyDefaultsRefModel
from app.db.models.infrastructuretype_ref_model import InfrastructureTypeRefModel


class InfrastructureTypeRefService:
    """
    Service layer for Infrastructure Type Reference operations.
    Handles business logic for infrastructure type reference data.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.infrastructure_type_repository = InfrastructureTypeRefRepository(session)

    async def get_all_infrastructure_types(self) -> Dict[str, Any]:
        """
        Get all infrastructure types.

        Business Logic:
        - Returns all non-deleted infrastructure types
        - Orders by name for consistent display
        - Converts model instances to dictionaries

        Returns:
            Dict with:
                - total: Total count of infrastructure types
                - infrastructure_types: List of infrastructure type dictionaries

        Example:
            >>> result = await service.get_all_infrastructure_types()
        """
        # Get all infrastructure types from repository
        infrastructure_types = await self.infrastructure_type_repository.get_all_infrastructure_types()

        # Convert model instances to dictionaries for response
        infrastructure_type_list = []
        for infra_type in infrastructure_types:
            infrastructure_type_list.append({
                "code": infra_type.code,
                "name": infra_type.name,
                "infra_vendor": infra_type.infra_vendor.value if infra_type.infra_vendor else None,
                "infra_family": infra_type.infra_family.value if infra_type.infra_family else None,
            })

        return {
            "total": len(infrastructure_type_list),
            "infrastructure_types": infrastructure_type_list
        }

    async def get_infrastructure_types_with_default_policies(self) -> Dict[str, Any]:
        """
        Get only infrastructure types that have at least one default policy.

        This is used for the Add Alert Policy Modal to ensure users can only
        create overrides for infrastructure types that have default policies.

        Business Logic:
        - Queries monitoring_policy_defaults_ref to find distinct infrastructure types
        - Only returns infrastructure types with is_deleted=false default policies
        - Orders by name for consistent display

        Returns:
            Dict with:
                - total: Count of infrastructure types with default policies
                - infrastructure_types: List of infrastructure type dictionaries

        Example:
            >>> result = await service.get_infrastructure_types_with_default_policies()
        """
        # Query to get distinct infrastructure types that have default policies
        stmt = (
            select(distinct(MonitoringPolicyDefaultsRefModel.infrastructuretype_ref_code))
            .where(MonitoringPolicyDefaultsRefModel.is_deleted == False)
        )

        result = await self.session.execute(stmt)
        infra_type_codes_with_defaults = result.scalars().all()

        # Now get the full infrastructure type details for these codes
        infrastructure_types = []
        for code in infra_type_codes_with_defaults:
            infra_type = await self.infrastructure_type_repository.get_by_code(code)
            if infra_type:
                infrastructure_types.append({
                    "code": infra_type.code,
                    "name": infra_type.name,
                    "infra_vendor": infra_type.infra_vendor.value if infra_type.infra_vendor else None,
                    "infra_family": infra_type.infra_family.value if infra_type.infra_family else None,
                })

        # Sort by name for consistent display
        infrastructure_types.sort(key=lambda x: x["name"])

        return {
            "total": len(infrastructure_types),
            "infrastructure_types": infrastructure_types
        }
