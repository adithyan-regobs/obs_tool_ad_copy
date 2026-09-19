"""
Logging wrapper for ToolNode.

Wraps LangGraph's ToolNode to add detailed logging for debugging
tool execution issues (e.g., infinite loops, error handling).
"""
import json
import logging
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.prebuilt import ToolNode

logger = logging.getLogger(__name__)

_DEFAULT_VALIDATE_PARAMS_STATE = {"tool_name": None, "valid": {}}


def _normalize_validate_params_state(raw_state: Any) -> dict:
    """Normalize ValidateParams state payload from graph/tool responses."""
    if not isinstance(raw_state, dict):
        return dict(_DEFAULT_VALIDATE_PARAMS_STATE)

    tool_name = raw_state.get("tool_name")
    if not isinstance(tool_name, str):
        tool_name = None

    valid = raw_state.get("valid")
    if not isinstance(valid, dict):
        valid = {}

    return {
        "tool_name": tool_name,
        "valid": valid,
    }


def _extract_tool_text_content(content: Any) -> str:
    """Extract MCP text payload from ToolMessage content."""
    if isinstance(content, list) and content:
        first_item = content[0]
        if isinstance(first_item, dict) and "text" in first_item:
            return str(first_item["text"])
    if isinstance(content, str):
        return content
    return str(content)


def create_logging_tool_node(tools):
    """
    Create a ToolNode wrapped with logging.

    Logs:
    - Tool name and arguments before execution
    - Result type, status, and content after execution
    - Any exceptions that occur

    Args:
        tools: List of tools to bind to the ToolNode

    Returns:
        Async function that can be used as a graph node
    """
    base_tool_node = ToolNode(tools, handle_tool_errors=True)

    # Tools that need tenant_id injected from graph state
    _TENANT_INJECTED_TOOLS = {
        "ValidateParams",
        "ReadEnvMaster", "ListEnvMaster",
        "ReadProductMaster", "ListProductMaster",
        "ReadRegionMaster", "ListRegionMaster",
        "ListDatabaseServers",
    }

    async def logging_tool_node(state):
        messages = state.get("messages", [])
        last_msg = messages[-1] if messages else None

        injected_validate_state = False
        injected_tenant_id = False
        validate_params_state = _normalize_validate_params_state(
            state.get("validate_params_state")
        )
        state_tenant_id = state.get("tenant_id")
        normalized_tenant_id = state_tenant_id.strip() if isinstance(state_tenant_id, str) else ""

        # Log incoming tool calls
        if last_msg and hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            for tc in last_msg.tool_calls:
                tool_name = tc.get("name")

                # Inject tenant_id for all tenant-scoped tools;
                # conversation_state only for ValidateParams.
                if tool_name in _TENANT_INJECTED_TOOLS:
                    args = tc.get("args")
                    if not isinstance(args, dict):
                        args = {}
                    if normalized_tenant_id:
                        args["tenant_id"] = normalized_tenant_id
                        injected_tenant_id = True
                    if tool_name == "ValidateParams":
                        args["conversation_state"] = validate_params_state
                        injected_validate_state = True
                    tc["args"] = args

                logger.info(
                    f"[TOOL_NODE] >>> Executing: {tc.get('name')} "
                    f"args={tc.get('args')}"
                )
        if injected_validate_state:
            logger.info(
                "[TOOL_NODE] Injected ValidateParams conversation_state: "
                f"tool_name={validate_params_state.get('tool_name')!r}, "
                f"valid_count={len(validate_params_state.get('valid', {}))}"
            )
        if injected_tenant_id:
            logger.info(
                "[TOOL_NODE] Injected tenant_id from graph state: "
                f"tenant_id={normalized_tenant_id!r}"
            )

        try:
            result = await base_tool_node.ainvoke(state)

            # Log result messages
            result_messages = result.get("messages", [])
            updated_validate_params_state = None
            for msg in result_messages:
                msg_type = type(msg).__name__
                content = getattr(msg, "content", None)
                status = getattr(msg, "status", "unknown")
                tool_call_id = getattr(msg, "tool_call_id", None)

                logger.info(
                    f"[TOOL_NODE] <<< Result: type={msg_type}, "
                    f"status={status}, tool_call_id={tool_call_id}"
                )
                if content:
                    content_str = str(content)[:500]
                    logger.info(f"[TOOL_NODE] <<< Content: {content_str}")

                if isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "ValidateParams":
                    payload_text = _extract_tool_text_content(content)
                    try:
                        payload = json.loads(payload_text) if payload_text else {}
                        if "conversation_state" in payload:
                            updated_validate_params_state = _normalize_validate_params_state(
                                payload["conversation_state"]
                            )
                            logger.info(
                                "[TOOL_NODE] Extracted ValidateParams conversation_state: "
                                f"tool_name={updated_validate_params_state.get('tool_name')!r}, "
                                f"valid_count={len(updated_validate_params_state.get('valid', {}))}"
                            )
                    except json.JSONDecodeError:
                        logger.warning(
                            "[TOOL_NODE] Failed to parse ValidateParams tool response as JSON; "
                            "keeping previous validate_params_state"
                        )

            if updated_validate_params_state is not None:
                return {
                    **result,
                    "validate_params_state": updated_validate_params_state,
                }

            return result

        except Exception as e:
            logger.error(
                f"[TOOL_NODE] !!! Exception: {type(e).__name__}: {e}",
                exc_info=True
            )
            raise

    return logging_tool_node
