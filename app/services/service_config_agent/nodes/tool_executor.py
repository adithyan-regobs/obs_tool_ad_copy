"""
Tool Executor Node for Service Config Agent.

Thin orchestrator that delegates to ToolExecutor service.
Single responsibility: wire state to executor and update state with results.
"""

from typing import Dict, Any, Protocol, runtime_checkable, List

from app.services.service_config_agent.state import ServiceConfigAgentState


@runtime_checkable
class ToolExecutorProtocol(Protocol):
    """Contract for tool execution."""

    async def execute(
        self,
        tool_calls: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Execute tool calls and return results."""
        ...


async def tool_executor_node(
    state: ServiceConfigAgentState,
    tool_executor: ToolExecutorProtocol,
) -> Dict[str, Any]:
    """
    Tool executor node - executes planned tool calls.

    Thin orchestrator that:
    1. Extracts tool calls from state
    2. Delegates to ToolExecutor
    3. Updates state with results

    Args:
        state: Current workflow state
        tool_executor: Injected ToolExecutor service

    Returns:
        State updates with tool_results
    """
    tool_calls = state.get("tool_calls", [])

    if not tool_calls:
        return {
            "tool_results": [],
            "error": "No tool calls to execute",
        }

    # Build context for executor
    context = state.get("context")
    executor_context = {
        "tenant_code": state.get("tenants_mst_code"),
        "environment": context.environment_enum if context else None,
        "geo_loc": context.geo_loc_mst_code if context else None,
    }

    # Delegate to executor
    results = await tool_executor.execute(
        tool_calls=tool_calls,
        context=executor_context,
    )

    # Check for all failures
    all_failed = all(
        not r.get("result", {}).get("success", False)
        for r in results
    )

    if all_failed and results:
        errors = [r.get("result", {}).get("error", "") for r in results]
        return {
            "tool_results": results,
            "error": f"All tools failed: {'; '.join(errors)}",
        }

    return {
        "tool_results": results,
    }
