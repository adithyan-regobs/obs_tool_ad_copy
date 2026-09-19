"""
Tool Executor for Service Config Agent.

Single responsibility: Execute planned tool calls.
Handles parallel execution and result aggregation.
"""

import asyncio
import time
from typing import Dict, Any, List, Optional

from app.core.enum import EnvironmentEnum
from app.services.langfuse_service import langfuse_service


def _to_environment_enum(value: Any) -> Optional[EnvironmentEnum]:
    """Convert string or enum to EnvironmentEnum."""
    if value is None:
        return None
    if isinstance(value, EnvironmentEnum):
        return value
    if isinstance(value, str):
        try:
            return EnvironmentEnum(value.lower())
        except ValueError:
            return None
    return None


class ToolExecutor:
    """
    Executes planned tool calls.

    Responsibilities:
    - Execute tool calls (parallel where possible)
    - Aggregate results
    - Handle errors gracefully
    """

    def __init__(self, tools: Dict[str, Any]):
        """
        Initialize with tool instances.

        Args:
            tools: Dict mapping tool names to tool instances
                   Each tool must have an async execute() method
        """
        self.tools = tools

    async def execute(
        self,
        tool_calls: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Execute a list of tool calls.

        Args:
            tool_calls: List of {"tool": "name", "args": {...}}
            context: Shared context (tenant_code, environment, geo_loc)

        Returns:
            List of {"tool": "name", "result": ToolResult}
        """
        if not tool_calls:
            return []

        start_time = time.perf_counter()

        # Execute all tools in parallel
        tasks = [
            self._execute_single(call, context)
            for call in tool_calls
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        processed_results = []
        for i, result in enumerate(results):
            tool_name = tool_calls[i].get("tool", "unknown")

            if isinstance(result, Exception):
                processed_results.append({
                    "tool": tool_name,
                    "result": {
                        "success": False,
                        "error": str(result),
                    }
                })
            else:
                processed_results.append({
                    "tool": tool_name,
                    "result": result,
                })

        latency_ms = (time.perf_counter() - start_time) * 1000

        # Fire-and-forget: Log execution
        asyncio.create_task(langfuse_service.log_llm_call(
            call_type="tool_execution",
            input_text=f"Executed {len(tool_calls)} tools",
            output_text=f"Success: {sum(1 for r in processed_results if r['result'].get('success', False))}/{len(tool_calls)}",
            latency_ms=latency_ms,
            metadata={
                "tools": [c.get("tool") for c in tool_calls],
            }
        ))

        return processed_results

    async def _execute_single(
        self,
        call: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a single tool call."""
        tool_name = call.get("tool")
        args = call.get("args", {})

        if tool_name not in self.tools:
            return {
                "success": False,
                "error": f"Unknown tool: {tool_name}",
            }

        tool = self.tools[tool_name]

        # Build merged args - only add context values if not already provided
        merged_args = dict(args)

        # Tools that need tenant_code (most tools except semantic_parameter_search)
        tools_needing_tenant = {
            "get_service_config",
            "get_parameter_value",
            "search_services_by_parameter",
            "compare_configs",
            "compare_environments",
            "get_deployment_status",
            "get_service_dependencies",
            "semantic_config_search",
            "get_recommendations",
            "validate_config",
            "get_available_listener_priority",
        }
        if tool_name in tools_needing_tenant and "tenant_code" not in merged_args:
            merged_args["tenant_code"] = context.get("tenant_code")

        # Tools that need environment (not semantic_parameter_search or compare_environments)
        tools_needing_environment = {
            "get_service_config",
            "get_parameter_value",
            "search_services_by_parameter",
            "compare_configs",
            "get_deployment_status",
            "get_service_dependencies",
            "semantic_config_search",
            "get_recommendations",
            "validate_config",
            "get_available_listener_priority",
        }
        if tool_name in tools_needing_environment and "environment" not in merged_args:
            merged_args["environment"] = context.get("environment")

        # Convert environment strings to EnvironmentEnum (handles LLM passing "staging" as string)
        if "environment" in merged_args:
            merged_args["environment"] = _to_environment_enum(merged_args["environment"])
        if "env_a" in merged_args:
            merged_args["env_a"] = _to_environment_enum(merged_args["env_a"])
        if "env_b" in merged_args:
            merged_args["env_b"] = _to_environment_enum(merged_args["env_b"])

        # Add geo_loc_code only for tools that need it (config/status tools, not search tools)
        # Search tools operate across all geo locations
        tools_needing_geo_loc = {
            "get_service_config",
            "get_parameter_value",
            "compare_configs",
            "compare_environments",
            "get_deployment_status",
            "get_service_dependencies",
            "get_available_listener_priority",
        }
        if tool_name in tools_needing_geo_loc and "geo_loc_code" not in merged_args:
            merged_args["geo_loc_code"] = context.get("geo_loc")

        # Execute tool
        try:
            result = await tool.execute(**merged_args)
            return result
        except TypeError as e:
            # Handle missing/extra arguments
            return {
                "success": False,
                "error": f"Invalid arguments for {tool_name}: {str(e)}",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Tool execution failed: {str(e)}",
            }
