from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, distinct

from app.repository.alerttype_ref_repository import AlertTypeRefRepository
from app.db.models.monitoring_policy_defaults_ref_model import MonitoringPolicyDefaultsRefModel


class AlertTypeRefService:
    """
    Service layer for Alert Type Reference operations.
    Handles business logic for alert type reference data.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.alert_type_repository = AlertTypeRefRepository(session)

    async def get_active_alert_types(self) -> Dict[str, Any]:
        """
        Get all active alert types.

        Business Logic:
        - Returns only active (is_active=True) and non-deleted alert types
        - Orders by name for consistent display
        - Converts model instances to dictionaries

        Returns:
            Dict with:
                - total: Total count of active alert types
                - alert_types: List of active alert type dictionaries

        Example:
            >>> result = await service.get_active_alert_types()
        """
        # Get all active alert types from repository
        alert_types = await self.alert_type_repository.get_active_alert_types()

        # Convert model instances to dictionaries for response
        alert_type_list = []
        for alert_type in alert_types:
            alert_type_list.append({
                "code": alert_type.code,
                "name": alert_type.name,
            })

        return {
            "total": len(alert_type_list),
            "alert_types": alert_type_list
        }

    async def get_alert_types_for_infrastructure(
        self,
        infrastructuretype_ref_code: str
    ) -> Dict[str, Any]:
        """
        Get only alert types that have default policies for a specific infrastructure type.

        This is used for the Add Alert Policy Modal to ensure users can only
        select alert types that have default policies for the selected infrastructure type.

        Business Logic:
        - Queries monitoring_policy_defaults_ref to find distinct alert types for the given infrastructure type
        - Only returns alert types with is_deleted=false default policies
        - Orders by name for consistent display

        Args:
            infrastructuretype_ref_code: The infrastructure type code to filter by

        Returns:
            Dict with:
                - total: Count of alert types with default policies for this infrastructure type
                - alert_types: List of alert type dictionaries

        Example:
            >>> result = await service.get_alert_types_for_infrastructure("ecs_fargate_infrastructuretype_ref")
        """
        # Query to get distinct alert types that have default policies for this infrastructure type
        stmt = (
            select(distinct(MonitoringPolicyDefaultsRefModel.alerttype_ref_code))
            .where(
                MonitoringPolicyDefaultsRefModel.infrastructuretype_ref_code == infrastructuretype_ref_code,
                MonitoringPolicyDefaultsRefModel.is_deleted == False
            )
        )

        result = await self.session.execute(stmt)
        alert_type_codes_with_defaults = result.scalars().all()

        # Now get the full alert type details for these codes
        alert_types = []
        for code in alert_type_codes_with_defaults:
            alert_type = await self.alert_type_repository.get_by_code(code)
            if alert_type:
                alert_types.append({
                    "code": alert_type.code,
                    "name": alert_type.name,
                })

        # Sort by name for consistent display
        alert_types.sort(key=lambda x: x["name"])

        return {
            "total": len(alert_types),
            "alert_types": alert_types
        }
