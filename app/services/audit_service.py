"""
Audit Service

Handles audit trail business logic for logging user actions (3-table normalized design).
"""
import logging
import json
from typing import Dict, Any, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.audit_log_repository import (
    AuditActorRepository,
    AuditEventRepository,
    AuditFieldChangeRepository,
)
from app.core.enum import AuditActionEnum

logger = logging.getLogger(__name__)


class AuditService:
    """Service layer for audit trail operations"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.actor_repository = AuditActorRepository(session)
        self.event_repository = AuditEventRepository(session)
        self.field_change_repository = AuditFieldChangeRepository(session)

    async def log_action(
        self,
        tenants_mst_code: str,
        action_type: AuditActionEnum,
        resource_type: str,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        role: Optional[str] = None,
        resource_id: Optional[str] = None,
        old_values: Optional[Dict[str, Any]] = None,
        new_values: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        event_source: Optional[str] = None,
        status: str = "success",
        correlation_id: Optional[str] = None,
        request_payload: Optional[Dict[str, Any]] = None,
        response_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log any action to audit trail.

        This is a fire-and-forget method - errors are logged but don't fail the main operation.

        Args:
            tenants_mst_code: Tenant identifier
            action_type: Type of action (CREATE, UPDATE, DELETE, LOGIN, etc.)
            resource_type: Type of resource being acted upon
            user_id: User ID (optional for system actions)
            username: Username/email for display
            role: User role at time of action
            resource_id: Code of the resource being acted upon (matches 'code' column)
            old_values: Previous values (for UPDATE operations)
            new_values: New values (for CREATE/UPDATE operations)
            ip_address: Client IP address
            user_agent: Client user agent
            event_source: Source of the event (API endpoint, background job, etc.)
            status: Event status (success, failure, pending)
            correlation_id: Correlation ID for distributed tracing
            request_payload: Sanitized request payload
            response_payload: Sanitized response payload
        """
        try:
            # Step 1: Get or create actor
            actor = await self.actor_repository.get_or_create_actor(
                user_id=user_id,
                username=username,
                role=role,
                ip_address=ip_address,
                user_agent=user_agent,
            )

            # Step 2: Create event
            event = await self.event_repository.create_event(
                actor_id=actor.actor_id,
                event_name=action_type,
                resource_type=resource_type,
                resource_id=resource_id,
                tenants_mst_code=tenants_mst_code,
                event_source=event_source,
                status=status,
                correlation_id=correlation_id,
                request_payload=request_payload,
                response_payload=response_payload,
            )

            # Step 3: Create field changes (if this is an UPDATE with old/new values)
            if old_values and new_values:
                field_changes_list = self._compute_field_changes(old_values, new_values)
                if field_changes_list:
                    await self.field_change_repository.create_field_changes_bulk(
                        event_id=event.event_id,
                        changes=field_changes_list,
                    )

            logger.info(
                f"Audit: {action_type.value} on {resource_type} by {username or 'SYSTEM'} "
                f"(event_id={event.event_id}, actor_id={actor.actor_id})"
            )

        except Exception as e:
            logger.error(f"Failed to create audit log: {str(e)}", exc_info=True)

    def _compute_field_changes(
        self,
        old_values: Dict[str, Any],
        new_values: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Compute field-level changes between old and new values.

        Returns:
            List of dicts with keys: field_name, old_value, new_value
        """
        field_changes = []

        # Check for changed or added fields
        for key, new_val in new_values.items():
            old_val = old_values.get(key)
            if old_val != new_val:
                field_changes.append({
                    'field_name': key,
                    'old_value': self._serialize_value(old_val),
                    'new_value': self._serialize_value(new_val),
                })

        # Check for removed fields
        for key in old_values:
            if key not in new_values:
                field_changes.append({
                    'field_name': key,
                    'old_value': self._serialize_value(old_values[key]),
                    'new_value': None,
                })

        return field_changes

    def _serialize_value(self, value: Any) -> Optional[str]:
        """Convert a value to a string representation for storage"""
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        # For complex types (dict, list, etc.), serialize as JSON
        try:
            return json.dumps(value, default=str)
        except Exception:
            return str(value)
