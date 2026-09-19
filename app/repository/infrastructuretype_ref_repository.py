from typing import Optional, List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.infrastructuretype_ref_model import InfrastructureTypeRefModel
from app.repository.base_repository import BaseRepository
from app.core.enum import InfraFamilyEnum, InfraVendorEnum


class InfrastructureTypeRefRepository(BaseRepository[InfrastructureTypeRefModel]):
    """Repository for Infrastructure Type Reference operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(InfrastructureTypeRefModel, session)

    async def get_by_code(self, code: str) -> Optional[InfrastructureTypeRefModel]:
        """
        Get infrastructure type by code.

        Args:
            code: Infrastructure type code (e.g., 'ec2', 'rds', 'lambda')

        Returns:
            InfrastructureTypeRefModel or None if not found

        Example:
            >>> infra_type = await repo.get_by_code('ec2')
        """
        stmt = select(self.model).where(self.model.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_infrastructure_types(self) -> List[InfrastructureTypeRefModel]:
        """
        Get all non-deleted infrastructure types.

        Returns:
            List of InfrastructureTypeRefModel instances

        Example:
            >>> all_types = await repo.get_all_infrastructure_types()
        """
        stmt = select(self.model).where(
            self.model.is_deleted == False
        ).order_by(self.model.name)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_vendor(
        self, infra_vendor: InfraVendorEnum
    ) -> List[InfrastructureTypeRefModel]:
        """
        Get all infrastructure types by vendor.

        Args:
            infra_vendor: Infrastructure vendor enum (e.g., InfraVendorEnum.aws)

        Returns:
            List of InfrastructureTypeRefModel instances for the vendor

        Example:
            >>> aws_types = await repo.get_by_vendor(InfraVendorEnum.aws)
        """
        stmt = select(self.model).where(
            self.model.infra_vendor == infra_vendor,
            self.model.is_deleted == False
        ).order_by(self.model.name)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_family(
        self, infra_family: InfraFamilyEnum
    ) -> List[InfrastructureTypeRefModel]:
        """
        Get all infrastructure types by family.

        Args:
            infra_family: Infrastructure family enum (e.g., InfraFamilyEnum.database)

        Returns:
            List of InfrastructureTypeRefModel instances for the family

        Example:
            >>> db_types = await repo.get_by_family(InfraFamilyEnum.database)
        """
        stmt = select(self.model).where(
            self.model.infra_family == infra_family,
            self.model.is_deleted == False
        ).order_by(self.model.name)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_vendor_and_family(
        self,
        infra_vendor: InfraVendorEnum,
        infra_family: InfraFamilyEnum
    ) -> List[InfrastructureTypeRefModel]:
        """
        Get all infrastructure types by vendor and family.

        Args:
            infra_vendor: Infrastructure vendor enum
            infra_family: Infrastructure family enum

        Returns:
            List of InfrastructureTypeRefModel instances matching both criteria

        Example:
            >>> aws_dbs = await repo.get_by_vendor_and_family(
            ...     InfraVendorEnum.aws,
            ...     InfraFamilyEnum.database
            ... )
        """
        stmt = select(self.model).where(
            self.model.infra_vendor == infra_vendor,
            self.model.infra_family == infra_family,
            self.model.is_deleted == False
        ).order_by(self.model.name)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_with_capability(
        self,
        has_metrics: Optional[bool] = None,
        has_log: Optional[bool] = None,
        has_traces: Optional[bool] = None
    ) -> List[InfrastructureTypeRefModel]:
        """
        Get infrastructure types by capability flags.

        Args:
            has_metrics: Filter by metrics capability
            has_log: Filter by log capability
            has_traces: Filter by traces capability

        Returns:
            List of InfrastructureTypeRefModel instances matching capabilities

        Example:
            >>> trace_enabled = await repo.get_with_capability(has_traces=True)
        """
        stmt = select(self.model).where(self.model.is_deleted == False)

        if has_metrics is not None:
            stmt = stmt.where(self.model.has_metrics == has_metrics)
        if has_log is not None:
            stmt = stmt.where(self.model.has_log == has_log)
        if has_traces is not None:
            stmt = stmt.where(self.model.has_traces == has_traces)

        stmt = stmt.order_by(self.model.name)
        result = await self.session.execute(stmt)
        return result.scalars().all()
