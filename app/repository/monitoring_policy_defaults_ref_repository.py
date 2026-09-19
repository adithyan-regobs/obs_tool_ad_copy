from typing import List, Optional, Dict, Any
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession


from app.db.models.monitoring_policy_defaults_ref_model import MonitoringPolicyDefaultsRefModel
from app.repository.base_repository import BaseRepository


class MonitoringPolicyDefaultsRefRepository(BaseRepository[MonitoringPolicyDefaultsRefModel]):
    """Repository for Monitoring Policy Defaults Reference operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(MonitoringPolicyDefaultsRefModel, session)

    async def get_by_code(self, code: str) -> Optional[MonitoringPolicyDefaultsRefModel]:
        """
        Get a default policy by its code.

        Args:
            code: The policy code (e.g., "alb_unhealthy_hosts_default")

        Returns:
            MonitoringPolicyDefaultsRefModel or None if not found
        """
        stmt = select(self.model).where(
            and_(
                self.model.code == code,
                self.model.is_deleted == False
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_infrastructuretype_ref_code(self, infrastructuretype_ref_code: str) -> List[MonitoringPolicyDefaultsRefModel]:
        """Get all monitoring policies by infrastructuretype_ref_code"""
        stmt = select(self.model).where(self.model.infrastructuretype_ref_code == infrastructuretype_ref_code)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_infra_and_alert_type(
        self,
        infrastructuretype_ref_code: str,
        alerttype_ref_code: str
    ) -> Optional[MonitoringPolicyDefaultsRefModel]:
        """
        Get a default policy by infrastructure type and alert type.

        Args:
            infrastructuretype_ref_code: Infrastructure type code
            alerttype_ref_code: Alert type code

        Returns:
            MonitoringPolicyDefaultsRefModel or None if not found
        """
        stmt = select(self.model).where(
            and_(
                self.model.infrastructuretype_ref_code == infrastructuretype_ref_code,
                self.model.alerttype_ref_code == alerttype_ref_code,
                self.model.is_deleted == False
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_defaults_with_filters(
        self,
        infrastructuretype_code: Optional[str] = None,
        alerttype_code: Optional[str] = None,
        skip: int = 0,
        limit: int = 100
    ) -> tuple[List[Dict[str, Any]], int]:
        """
        Get all default monitoring policies with optional filters and pagination.

        Args:
            infrastructuretype_code: Optional filter by infrastructure type
            alerttype_code: Optional filter by alert type
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            Tuple of (list of policies as dicts, total count)
        """
        # Build where conditions
        conditions = [self.model.is_deleted == False]

        if infrastructuretype_code:
            conditions.append(self.model.infrastructuretype_ref_code == infrastructuretype_code)

        if alerttype_code:
            conditions.append(self.model.alerttype_ref_code == alerttype_code)

        # Count query
        count_stmt = select(func.count()).select_from(self.model).where(and_(*conditions))
        count_result = await self.session.execute(count_stmt)
        total = count_result.scalar() or 0

        # Data query with pagination
        stmt = (
            select(self.model)
            .where(and_(*conditions))
            .order_by(
                self.model.infrastructuretype_ref_code,
                self.model.alerttype_ref_code
            )
            .offset(skip)
            .limit(limit)
        )

        result = await self.session.execute(stmt)
        policies = result.scalars().all()

        # Convert to dictionaries
        policies_dict = []
        for policy in policies:
            policies_dict.append({
                "code": policy.code,
                "name": policy.name,
                "description": policy.description,
                "infrastructuretype_ref_code": policy.infrastructuretype_ref_code,
                "alerttype_ref_code": policy.alerttype_ref_code,
                "comparator": policy.comparator,
                "threshold_value": policy.threshold_value,
                "threshold_unit": policy.threshold_unit,
                "eval_window": policy.eval_window,
                "for_duration": policy.for_duration,
                "no_data": policy.no_data,
                "severity": policy.severity,
                "is_active": policy.is_active,
                "created_at": policy.created_at,
                "updated_at": policy.updated_at
            })

        return policies_dict, total
