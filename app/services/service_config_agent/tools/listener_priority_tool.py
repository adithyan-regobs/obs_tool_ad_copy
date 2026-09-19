"""
Listener Priority Tool for Service Config Agent.

Single responsibility: Get available listener priority values.
Thin orchestrator - delegates calculation to priority_calculator.
"""

from typing import List, Optional, Protocol, runtime_checkable

from app.core.enum import EnvironmentEnum
from app.services.service_config_agent.tools.base import ToolResult
from app.services.service_config_agent.tools.priority_calculator import (
    find_next_available_priority,
    validate_priority_available,
    suggest_alternative,
)


@runtime_checkable
class ListenerPriorityRepositoryProtocol(Protocol):
    """Contract for listener priority repository."""

    async def get_used_listener_priorities(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
        exclude_service_code: Optional[str] = None,
    ) -> List[int]:
        """Get all used listener priorities in scope."""
        ...


class GetAvailableListenerPriority:
    """
    Tool to get an available listener_rule_priority value.

    Handles two scenarios:
    1. No proposed_value: Returns next available priority
    2. With proposed_value: Validates and confirms or suggests alternative

    Thin orchestrator that:
    1. Delegates data access to repository
    2. Delegates calculation to priority_calculator
    """

    def __init__(self, repository: ListenerPriorityRepositoryProtocol):
        """
        Initialize with repository dependency.

        Args:
            repository: Repository for DB access
        """
        self._repo = repository

    async def execute(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
        proposed_value: Optional[int] = None,
        current_service_code: Optional[str] = None,
    ) -> ToolResult:
        """
        Get available listener priority.

        Args:
            tenant_code: Tenant code
            environment: Environment (dev/staging/prod)
            geo_loc_code: Geographic location
            proposed_value: Optional value user wants to validate
            current_service_code: Exclude this service (for updates)

        Returns:
            ToolResult with suggested_value for autofill
        """
        # Query used priorities from repository
        used_priorities = await self._repo.get_used_listener_priorities(
            tenant_code=tenant_code,
            environment=environment,
            geo_loc_code=geo_loc_code,
            exclude_service_code=current_service_code,
        )

        if proposed_value is not None:
            return self._handle_validation(proposed_value, used_priorities)
        else:
            return self._handle_discovery(used_priorities)

    def _handle_validation(
        self,
        proposed_value: int,
        used_priorities: List[int],
    ) -> ToolResult:
        """
        Handle validation mode - check if proposed value is available.

        Args:
            proposed_value: The value to validate
            used_priorities: List of used priorities

        Returns:
            ToolResult with validation status
        """
        is_available, error = validate_priority_available(
            proposed_value, used_priorities
        )

        if is_available:
            return ToolResult(
                success=True,
                data={
                    "parameter": "listener_rule_priority",
                    "proposed_value": proposed_value,
                    "status": "available",
                    "suggested_value": proposed_value,
                    "message": f"Priority {proposed_value} is available.",
                },
            )
        else:
            # Suggest alternative close to proposed value
            alternative = suggest_alternative(proposed_value, used_priorities)

            # Find nearby used priorities for context
            nearby = [
                p for p in used_priorities
                if abs(p - proposed_value) <= 20
            ][:5]

            return ToolResult(
                success=True,
                data={
                    "parameter": "listener_rule_priority",
                    "proposed_value": proposed_value,
                    "status": "conflict",
                    "suggested_value": alternative,
                    "message": f"{error}. Suggested alternative: {alternative}",
                    "used_priorities_nearby": nearby,
                },
            )

    def _handle_discovery(
        self,
        used_priorities: List[int],
    ) -> ToolResult:
        """
        Handle discovery mode - find next available priority.

        Args:
            used_priorities: List of used priorities

        Returns:
            ToolResult with suggested priority
        """
        next_priority = find_next_available_priority(used_priorities)

        # Build range info for context
        if used_priorities:
            range_info = f"{min(used_priorities)}-{max(used_priorities)}"
        else:
            range_info = "No priorities in use"

        return ToolResult(
            success=True,
            data={
                "parameter": "listener_rule_priority",
                "suggested_value": next_priority,
                "status": "suggested",
                "message": f"Suggested priority: {next_priority}",
                "total_used": len(used_priorities),
                "range": range_info,
            },
        )
