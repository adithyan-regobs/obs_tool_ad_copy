"""
Repository for Audit Trail operations (3-table normalized design)
"""
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select, func, and_
from sqlalchemy.orm import selectinload

from app.db.models.audit_log_model import AuditActorModel, AuditEventModel, AuditFieldChangeModel
from app.repository.base_repository import BaseRepository
from app.core.enum import AuditActionEnum


class AuditActorRepository(BaseRepository[AuditActorModel]):
    """Repository for Audit Actor operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(AuditActorModel, session)

    async def get_or_create_actor(
        self,
        user_id: Optional[int],
        username: Optional[str],
        role: Optional[str],
        ip_address: Optional[str],
        user_agent: Optional[str],
    ) -> AuditActorModel:
        """
        Create a new actor for each request.

        Note: We create a new actor for every request rather than reusing actors.
        This keeps the audit trail simple and avoids PostgreSQL INET type comparison issues.
        Disk is cheap, and this provides the most accurate audit trail.
        """
        return await self.create(
            user_id=user_id,
            username=username,
            role=role,
            ip_address=ip_address,
            user_agent=user_agent,
        )


class AuditEventRepository(BaseRepository[AuditEventModel]):
    """Repository for Audit Event operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(AuditEventModel, session)

    async def generate_event_id(self) -> int:
        """
        Generate a new event_id from the sequence without creating the event yet.
        Used by shared connection approach to set event_id before UPDATE happens.

        Returns:
            The next event_id from the audit_event_event_id_seq sequence
        """
        result = await self.session.execute(
            text("SELECT nextval('audit_event_event_id_seq')")
        )
        return result.scalar()

    async def create_event(
        self,
        actor_id: int,
        event_name: AuditActionEnum,
        resource_type: str,
        tenants_mst_code: str,
        resource_id: Optional[str] = None,
        event_source: Optional[str] = None,
        status: Optional[str] = None,
        correlation_id: Optional[str] = None,
        request_payload: Optional[Dict[str, Any]] = None,
        response_payload: Optional[Dict[str, Any]] = None,
    ) -> AuditEventModel:
        """Create a new audit event"""
        return await self.create(
            actor_id=actor_id,
            event_name=event_name,
            resource_type=resource_type,
            resource_id=resource_id,
            event_source=event_source,
            status=status,
            correlation_id=correlation_id,
            tenants_mst_code=tenants_mst_code,
            request_payload=request_payload,
            response_payload=response_payload,
        )

    async def create_event_with_id(
        self,
        event_id: int,
        actor_id: int,
        event_name: AuditActionEnum,
        resource_type: str,
        tenants_mst_code: str,
        resource_id: Optional[str] = None,
        event_source: Optional[str] = None,
        status: Optional[str] = None,
        correlation_id: Optional[str] = None,
        request_payload: Optional[Dict[str, Any]] = None,
        response_payload: Optional[Dict[str, Any]] = None,
    ) -> AuditEventModel:
        """
        Create a new audit event with a pre-generated event_id.
        Used by shared connection approach where event_id is generated early.

        Args:
            event_id: Pre-generated event_id from generate_event_id()
            ... (other standard audit event fields)

        Returns:
            The created AuditEventModel instance
        """
        db_obj = AuditEventModel(
            event_id=event_id,
            actor_id=actor_id,
            event_name=event_name,
            resource_type=resource_type,
            resource_id=resource_id,
            event_source=event_source,
            status=status,
            correlation_id=correlation_id,
            tenants_mst_code=tenants_mst_code,
            request_payload=request_payload,
            response_payload=response_payload,
        )
        self.session.add(db_obj)
        await self.session.flush()
        await self.session.refresh(db_obj)
        return db_obj

    async def get_events_by_tenant_paginated(
        self,
        tenant_code: str,
        page: int = 1,
        page_size: int = 50,
        event_name: Optional[AuditActionEnum] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> Tuple[List[AuditEventModel], int]:
        """
        Get paginated audit events for a tenant with filtering.

        Args:
            tenant_code: Tenant code for data isolation
            page: Page number (starts at 1)
            page_size: Number of events per page (max 100)
            event_name: Filter by event type (CREATE, UPDATE, DELETE, READ)
            date_from: Filter events after this date (inclusive)
            date_to: Filter events before this date (inclusive)

        Returns:
            Tuple of (events_list, total_count)
        """
        # Validate pagination
        page = max(1, page)
        page_size = min(100, max(1, page_size))
        offset = (page - 1) * page_size

        # Build WHERE conditions
        conditions = [AuditEventModel.tenants_mst_code == tenant_code]

        if event_name:
            conditions.append(AuditEventModel.event_name == event_name)

        if date_from:
            conditions.append(AuditEventModel.event_time >= date_from)

        if date_to:
            conditions.append(AuditEventModel.event_time <= date_to)

        # Build query with joins
        stmt = (
            select(AuditEventModel)
            .join(AuditActorModel, AuditEventModel.actor_id == AuditActorModel.actor_id)
            .options(
                selectinload(AuditEventModel.actor),
                selectinload(AuditEventModel.field_changes)
            )
            .where(and_(*conditions))
            .order_by(AuditEventModel.event_time.desc())
            .offset(offset)
            .limit(page_size)
        )

        # Execute query
        result = await self.session.execute(stmt)
        events = result.scalars().all()

        # Get total count
        count_stmt = (
            select(func.count(AuditEventModel.event_id))
            .where(and_(*conditions))
        )
        count_result = await self.session.execute(count_stmt)
        total_count = count_result.scalar()

        return list(events), total_count

    async def get_event_by_id_with_details(
        self, event_id: int, tenant_code: str
    ) -> Optional[AuditEventModel]:
        """
        Get a single audit event with all details (actor + field changes).
        Ensures tenant isolation.

        Args:
            event_id: Event ID to fetch
            tenant_code: Tenant code for security check

        Returns:
            AuditEventModel or None if not found or doesn't belong to tenant
        """
        stmt = (
            select(AuditEventModel)
            .options(
                selectinload(AuditEventModel.actor),
                selectinload(AuditEventModel.field_changes)
            )
            .where(
                and_(
                    AuditEventModel.event_id == event_id,
                    AuditEventModel.tenants_mst_code == tenant_code
                )
            )
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


class AuditFieldChangeRepository(BaseRepository[AuditFieldChangeModel]):
    """Repository for Audit Field Change operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(AuditFieldChangeModel, session)

    async def create_field_change(
        self,
        event_id: int,
        field_name: str,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
    ) -> AuditFieldChangeModel:
        """Create a new field change record"""
        return await self.create(
            event_id=event_id,
            field_name=field_name,
            old_value=old_value,
            new_value=new_value,
        )

    async def create_field_changes_bulk(
        self,
        event_id: int,
        changes: List[Dict[str, Any]],
    ) -> List[AuditFieldChangeModel]:
        """
        Create multiple field change records at once.

        Args:
            event_id: The event ID these changes belong to
            changes: List of dicts with keys: field_name, old_value, new_value
        """
        field_changes = []
        for change in changes:
            field_change = await self.create_field_change(
                event_id=event_id,
                field_name=change['field_name'],
                old_value=change.get('old_value'),
                new_value=change.get('new_value'),
            )
            field_changes.append(field_change)
        return field_changes
