"""
Audit Trail Service

Service layer for audit trail operations.
Handles business logic for fetching and displaying audit events.
"""
import logging
import math
from typing import Optional
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.audit_log_repository import AuditEventRepository
from app.schemas.audit_trail_schemas import (
    AuditTrailListResponse,
    AuditEventResponse,
    AuditEventDetailResponse,
    AuditFieldChangeResponse,
    AuditActorResponse,
)
from app.core.enum import AuditActionEnum

logger = logging.getLogger(__name__)


class AuditTrailService:
    """
    Service layer for Audit Trail operations.
    Handles business logic for audit event viewing and filtering.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.audit_event_repository = AuditEventRepository(session)

    async def get_audit_trail_paginated(
        self,
        tenant_code: str,
        page: int = 1,
        page_size: int = 50,
        event_name: Optional[AuditActionEnum] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> AuditTrailListResponse:
        """
        Get paginated audit trail for a tenant with filtering.

        Business Logic:
        - Fetches events from repository with tenant isolation
        - Transforms models to response schemas
        - Calculates pagination metadata (total_pages)
        - Converts IP addresses to strings for JSON serialization

        Args:
            tenant_code: Tenant code for data isolation
            page: Page number (starts at 1)
            page_size: Number of events per page (max 100)
            event_name: Filter by event type (CREATE, UPDATE, DELETE, READ)
            date_from: Filter events after this date (inclusive)
            date_to: Filter events before this date (inclusive)

        Returns:
            AuditTrailListResponse with paginated events and metadata

        Example:
            >>> service = AuditTrailService(session)
            >>> result = await service.get_audit_trail_paginated(
            ...     tenant_code="TNT001",
            ...     page=1,
            ...     page_size=50,
            ...     event_name=AuditActionEnum.UPDATE
            ... )
        """
        logger.info(
            f"[AUDIT TRAIL SERVICE] Fetching events for tenant {tenant_code}, "
            f"page={page}, page_size={page_size}, event_name={event_name}"
        )

        # Fetch paginated events from repository
        events, total_count = await self.audit_event_repository.get_events_by_tenant_paginated(
            tenant_code=tenant_code,
            page=page,
            page_size=page_size,
            event_name=event_name,
            date_from=date_from,
            date_to=date_to,
        )

        # Calculate total pages
        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0

        # Transform models to response schemas
        event_responses = []
        for event in events:
            # Build field changes list
            field_changes = [
                AuditFieldChangeResponse(
                    id=fc.id,
                    field_name=fc.field_name,
                    old_value=fc.old_value,
                    new_value=fc.new_value,
                )
                for fc in event.field_changes
            ]

            # Build event response with actor info
            event_response = AuditEventResponse(
                event_id=event.event_id,
                event_time=event.event_time,
                event_name=event.event_name,
                event_source=event.event_source,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                status=event.status,
                correlation_id=event.correlation_id,
                # Actor information (from join)
                username=event.actor.username if event.actor else None,
                role=event.actor.role if event.actor else None,
                ip_address=str(event.actor.ip_address) if event.actor and event.actor.ip_address else None,
                user_agent=event.actor.user_agent if event.actor else None,
                # Field changes
                field_changes=field_changes,
            )
            event_responses.append(event_response)

        logger.info(
            f"[AUDIT TRAIL SERVICE] Returning {len(event_responses)} events "
            f"(total: {total_count}, page: {page}/{total_pages})"
        )

        return AuditTrailListResponse(
            events=event_responses,
            total=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    async def get_audit_event_detail(
        self,
        event_id: int,
        tenant_code: str
    ) -> Optional[AuditEventDetailResponse]:
        """
        Get detailed view of a single audit event.

        Business Logic:
        - Fetches event with tenant isolation check
        - Transforms model to detailed response schema
        - Includes full actor details and field changes
        - Includes request/response payloads

        Args:
            event_id: Event ID to fetch
            tenant_code: Tenant code for security check

        Returns:
            AuditEventDetailResponse or None if not found

        Example:
            >>> service = AuditTrailService(session)
            >>> event = await service.get_audit_event_detail(
            ...     event_id=185,
            ...     tenant_code="TNT001"
            ... )
        """
        logger.info(
            f"[AUDIT TRAIL SERVICE] Fetching event {event_id} for tenant {tenant_code}"
        )

        # Fetch event with tenant isolation check
        event = await self.audit_event_repository.get_event_by_id_with_details(
            event_id=event_id,
            tenant_code=tenant_code
        )

        if not event:
            logger.warning(
                f"Event {event_id} not found or doesn't belong to tenant {tenant_code}"
            )
            return None

        # Build actor response
        actor_response = AuditActorResponse(
            actor_id=event.actor.actor_id,
            username=event.actor.username,
            role=event.actor.role,
            ip_address=str(event.actor.ip_address) if event.actor.ip_address else None,
            user_agent=event.actor.user_agent,
        )

        # Build field changes list
        field_changes = [
            AuditFieldChangeResponse(
                id=fc.id,
                field_name=fc.field_name,
                old_value=fc.old_value,
                new_value=fc.new_value,
            )
            for fc in event.field_changes
        ]

        # Build detailed response
        return AuditEventDetailResponse(
            event_id=event.event_id,
            event_time=event.event_time,
            event_name=event.event_name,
            event_source=event.event_source,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            status=event.status,
            correlation_id=event.correlation_id,
            actor=actor_response,
            field_changes=field_changes,
            request_payload=event.request_payload,
            response_payload=event.response_payload,
        )
