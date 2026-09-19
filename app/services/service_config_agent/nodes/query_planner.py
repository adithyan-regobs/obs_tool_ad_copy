"""
Query Planner Node for Service Config Agent.

Thin orchestrator that delegates to QueryPlanner service.
Single responsibility: wire state to planner and update state with plan.
"""

from typing import Dict, Any, Protocol, runtime_checkable, List

from app.services.service_config_agent.state import ServiceConfigAgentState


@runtime_checkable
class QueryPlannerProtocol(Protocol):
    """Contract for query planning."""

    async def plan(
        self,
        query: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Plan tool calls for a query."""
        ...


async def query_planner_node(
    state: ServiceConfigAgentState,
    query_planner: QueryPlannerProtocol,
) -> Dict[str, Any]:
    """
    Query planner node - plans which tools to call.

    Thin orchestrator that:
    1. Extracts context from state
    2. Delegates to QueryPlanner
    3. Updates state with tool calls

    Args:
        state: Current workflow state
        query_planner: Injected QueryPlanner service

    Returns:
        State updates with tool_calls
    """
    # Build context for planner
    context = state.get("context")
    planner_context = {
        "service_name": context.service_code if context else None,
        "environment": context.environment_enum.value if context and context.environment_enum else None,
        "geo_loc": context.geo_loc_mst_code if context else None,
    }

    # Delegate to planner
    plan = await query_planner.plan(
        query=state["user_message"],
        context=planner_context,
        history=state.get("history", []),
    )

    # Check for planning errors
    if plan.get("error"):
        return {
            "tool_calls": [],
            "error": plan.get("reasoning", "Failed to plan query"),
        }

    return {
        "tool_calls": plan.get("tool_calls", []),
    }
