from typing import Optional, List
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.datadog_alert_query_ref_model import DatadogAlertQueryRefModel
from app.repository.base_repository import BaseRepository
from app.core.enum import SignalKindEnum

class DatadogAlertQueryRefRepository(BaseRepository[DatadogAlertQueryRefModel]):
    """Repository for Datadog Alert Query Reference operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(DatadogAlertQueryRefModel, session)

    async def get_by_infra_alert_signal(
        self,
        infrastructuretype_ref_code: str,
        alerttype_ref_code: str,
        signal_kind: SignalKindEnum
    ) -> Optional[DatadogAlertQueryRefModel]:
        """
        Get query template by infrastructure type, alert type, and signal kind.

        Args:
            infrastructuretype_ref_code: Infrastructure type (ec2, rds, lambda, etc.)
            alerttype_ref_code: Alert type (cpu_utilization, memory_usage, etc.)
            signal_kind: Signal kind enum (metric, log, trace)

        Returns:
            DatadogAlertQueryRefModel if found, None otherwise
        """
        stmt = select(self.model).where(
            and_(
                self.model.infrastructuretype_ref_code == infrastructuretype_ref_code,
                self.model.alerttype_ref_code == alerttype_ref_code,
                self.model.signal_kind == signal_kind
            )
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_queries(
        self,
        infrastructuretype_ref_code: Optional[str] = None,
        alerttype_ref_code: Optional[str] = None,
        signal_kind: Optional[SignalKindEnum] = None,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: int = 100
    ) -> List[DatadogAlertQueryRefModel]:
        """
        Get all Datadog alert query templates with optional filtering and pagination.

        Args:
            infrastructuretype_ref_code: Optional filter by infrastructure type
            alerttype_ref_code: Optional filter by alert type
            signal_kind: Optional filter by signal kind enum
            is_active: Optional filter by active status
            skip: Pagination offset
            limit: Page size

        Returns:
            List of DatadogAlertQueryRefModel instances

        Example:
            >>> queries = await repo.get_all_queries(
            ...     infrastructuretype_ref_code='ec2',
            ...     is_active=True,
            ...     skip=0,
            ...     limit=50
            ... )
        """
        stmt = select(self.model).where(self.model.is_deleted == False)

        # Apply optional filters
        if infrastructuretype_ref_code:
            stmt = stmt.where(self.model.infrastructuretype_ref_code == infrastructuretype_ref_code)
        if alerttype_ref_code:
            stmt = stmt.where(self.model.alerttype_ref_code == alerttype_ref_code)
        if signal_kind:
            stmt = stmt.where(self.model.signal_kind == signal_kind)
        if is_active is not None:
            stmt = stmt.where(self.model.is_active == is_active)

        # Order by name for consistent results
        stmt = stmt.order_by(self.model.name)

        # Apply pagination
        stmt = stmt.offset(skip).limit(limit)

        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def count_queries(
        self,
        infrastructuretype_ref_code: Optional[str] = None,
        alerttype_ref_code: Optional[str] = None,
        signal_kind: Optional[SignalKindEnum] = None,
        is_active: Optional[bool] = None
    ) -> int:
        """
        Count Datadog alert query templates with optional filtering.

        Args:
            infrastructuretype_ref_code: Optional filter by infrastructure type
            alerttype_ref_code: Optional filter by alert type
            signal_kind: Optional filter by signal kind enum
            is_active: Optional filter by active status

        Returns:
            Total count of matching records

        Example:
            >>> total = await repo.count_queries(is_active=True)
        """
        stmt = select(func.count()).select_from(self.model).where(
            self.model.is_deleted == False
        )

        # Apply same filters as get_all_queries
        if infrastructuretype_ref_code:
            stmt = stmt.where(self.model.infrastructuretype_ref_code == infrastructuretype_ref_code)
        if alerttype_ref_code:
            stmt = stmt.where(self.model.alerttype_ref_code == alerttype_ref_code)
        if signal_kind:
            stmt = stmt.where(self.model.signal_kind == signal_kind)
        if is_active is not None:
            stmt = stmt.where(self.model.is_active == is_active)

        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def get_by_code(self, code: str) -> Optional[DatadogAlertQueryRefModel]:
        """
        Get query template by code.

        Args:
            code: Query template code

        Returns:
            DatadogAlertQueryRefModel if found, None otherwise

        Example:
            >>> query = await repo.get_by_code('ec2_cpu_metric')
        """
        stmt = select(self.model).where(self.model.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_query(
        self,
        query: DatadogAlertQueryRefModel,
        name: Optional[str] = None,
        code: Optional[str] = None,
        infrastructuretype_ref_code: Optional[str]=None,
        alerttype_ref_code: Optional[str]=None,
         signal_kind: Optional[SignalKindEnum]=None,
        description: Optional[str] = None,
        query_template: Optional[str] = None,
        is_active: Optional[bool] = None
    ) -> DatadogAlertQueryRefModel:
        """
        Update existing Datadog alert query template.

        Args:
            query: Existing query model to update
            name: Optional new name
            description: Optional new description
            query_template: Optional new query template
            is_active: Optional new active status

        Returns:
            Updated DatadogAlertQueryRefModel

        Example:
            >>> existing_query = await repo.get_by_code('ec2_cpu_metric')
            >>> updated = await repo.update_query(
            ...     existing_query,
            ...     name='New Name',
            ...     is_active=False
            ... )
        """
        updates = {}

        if name is not None:
            updates['name'] = name
        if code is not None:
            updates['code'] = code
        if infrastructuretype_ref_code is not None:
            updates['infrastructuretype_ref_code'] = infrastructuretype_ref_code
        if alerttype_ref_code is not None:
            updates['alerttype_ref_code'] = alerttype_ref_code
        if signal_kind is not None:
            updates['signal_kind'] = signal_kind
        if description is not None:
            updates['description'] = description
        if query_template is not None:
            updates['query_template'] = query_template
        if is_active is not None:
            updates['is_active'] = is_active
 
        return await self.update(query, updates)
