"""
Response handler node for CREATE and REFERENCE workflows.

Generates user-facing messages based on validation state and reference results.
"""
import json
import logging
from typing import Dict, Any, List, Optional
from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.utils.graph_utils import get_tenant_id
from app.infra_chat_agent.utils.chat_history_util import save_conversation_turn
from app.infra_chat_agent.infra_chat_constants import InfraChatConstants
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.infra_chat_agent.config.config_models import (
    TenantId, InfraTypeCode, StaticSource, ConditionalRequirement, ParameterMeta
)
from app.infra_chat_agent.llm_config_util import LLMConfigUtil

logger = logging.getLogger(__name__)


def _sanitize_placement_parameters(raw: Any) -> Dict[str, Any]:
    """Keep only frontend-required placement fields and normalize legacy aliases."""
    if not isinstance(raw, dict):
        return {}

    def _first_non_empty(*keys: str):
        for key in keys:
            value = raw.get(key)
            if value is not None and value != "":
                return value
        return None

    normalized = {
        "infra_vendor_enum": _first_non_empty("infra_vendor_enum", "infra_vendor"),
        "applications_mst_code": _first_non_empty("applications_mst_code"),
        "environment_enum": _first_non_empty("environment_enum", "environment"),
        "geo_loc": _first_non_empty("geo_loc"),
        "geo_loc_mst_code": _first_non_empty("geo_loc_mst_code", "geo_loc_code"),
        "case_type_ref_code": _first_non_empty("case_type_ref_code"),
        "case_ref_code": _first_non_empty("case_ref_code", "case_code"),
        "service_mst_code": _first_non_empty("service_mst_code"),
        "product_name": _first_non_empty("product_name"),
    }

    return {
        key: value for key, value in normalized.items()
        if value is not None and value != ""
    }


def _get_extraction_feedback(state: ChatState) -> str:
    """Return prefix message if no params extracted on subsequent round."""
    slot_parameters = state.get("slot_parameters", {})
    has_prompted = state.get("has_prompted_for_params", False)
    if has_prompted and not slot_parameters:
        return "I couldn't extract any parameters from your message. Try providing values like `Parameter Name: value`.\n\n"
    return ""


def _get_conditional_reason(
    param: ParameterMeta,
    collected_params: Dict[str, Any]
) -> Optional[str]:
    """
    Get the reason message if a parameter is conditionally required.

    Args:
        param: The parameter metadata to check
        collected_params: All currently collected parameter values

    Returns:
        The conditional message if requirement is triggered, None otherwise
    """
    if not param.conditional:
        return None

    # Get the value of the dependent parameter
    dep_value = collected_params.get(param.conditional.if_param)

    # Check if the dependent value matches any of the trigger values
    if dep_value in param.conditional.has_value:
        return param.conditional.message

    return None


# ============================================================================
# REFERENCE Response Formatters
# ============================================================================

def _format_list_services_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Format list_services result with text summary and structured data for UI."""
    services = result.get("result", [])
    matched_services = result.get("matched_services", services)
    count = result.get("count", 0)
    infra_suffix = result.get("infra_suffix", "")

    if not services:
        return {
            "text": "No services found.",
            "matched_services": [],
        }

    # Build summary text matching service_config_chat format
    # Example: "Here are 72 AWS ecs_ec2 services with configs. Select one to view its configuration."
    text = f"Here are {count}{infra_suffix} services with configs. Select one to view its configuration."

    return {
        "text": text,
        "matched_services": matched_services,
    }


def _format_list_config_result(result: Dict[str, Any]) -> str:
    """Format list_config_of_service result."""
    # Check for errors
    if "error" in result:
        return f"Error: {result['error']}"

    # Check for warnings (fuzzy match)
    if "warning" in result:
        return f"Warning: {result['warning']}"

    service_data = result.get("result")
    if not service_data:
        return "Error: No service data found."

    lines = [
        f"Configuration for {service_data['name']}:",
        "",
        f"  CPU: {service_data['cpu']} cores",
        f"  Memory: {service_data['memory']} GB",
        f"  Description: {service_data['description']}",
    ]

    return "\n".join(lines)


def _format_list_param_result(result: Dict[str, Any]) -> str:
    """Format list_a_param_of_service result."""
    # Check for errors
    if "error" in result:
        return f"Error: {result['error']}"

    # Check for warnings (fuzzy match)
    if "warning" in result:
        return f"Warning: {result['warning']}"

    service_name = result.get("service_name", "")
    param_name = result.get("parameter_name", "")
    param_value = result.get("result")

    return f"{service_name} {param_name}: **{param_value}**"


def _handle_reference_response(state: ChatState) -> Dict[str, Any]:
    """
    Handle response generation for REFERENCE intent.

    With new MCP tools pattern:
    1. Check messages for tool results and LLM responses
    2. Parse tool results for UI compatibility (matched_services)
    3. Return LLM's final response (this gets saved to DB via save_conversation_turn)

    Note: Tool messages (AIMessage with tool_calls, ToolMessage) are ephemeral
    within-request context. Only the final turn_user_response gets persisted.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    # If reference_node already parsed JSON with remaining_reference_parameters, pass through
    remaining_ref_params = state.get("remaining_reference_parameters")
    logger.info(f"[REFERENCE_RESPONSE] remaining_reference_parameters in state: {bool(remaining_ref_params)}")
    if remaining_ref_params:
        turn_response = state.get("turn_user_response", "")
        logger.info(f"[REFERENCE_RESPONSE] Early return with turn_user_response: {turn_response[:100] if turn_response else 'EMPTY'}")
        return {
            "turn_user_response": turn_response,
            "remaining_reference_parameters": remaining_ref_params,
        }

    messages = state.get("messages", [])

    # NEW: Handle tool-based flow (messages from reference_node)
    if messages:
        # Find the last AI message (final response from LLM)
        last_ai_response = None
        last_tool_result = None
        last_tool_name = None

        for msg in reversed(messages):
            # AIMessage without tool_calls = final response
            if isinstance(msg, AIMessage) and msg.content and not getattr(msg, "tool_calls", None):
                last_ai_response = msg.content
                break

            # ToolMessage = tool execution result (for UI compatibility)
            if isinstance(msg, ToolMessage) and last_tool_result is None:
                last_tool_result = msg.content
                last_tool_name = getattr(msg, "name", "")

        # If LLM provided a final response, use it (this is what gets saved to DB)
        if last_ai_response:
            result = {"turn_user_response": last_ai_response}

            # Add matched_services if we have list_services tool result (for UI buttons)
            if last_tool_name == "list_services" and last_tool_result:
                try:
                    tool_data = json.loads(last_tool_result)
                    matched_services = tool_data.get("matched_services", tool_data.get("result", []))
                    if matched_services:
                        result["matched_services"] = matched_services
                except (json.JSONDecodeError, TypeError):
                    pass

            return result

        # If we only have tool result but no LLM response, format it
        if last_tool_result:
            try:
                tool_data = json.loads(last_tool_result)

                if last_tool_name == "list_services":
                    formatted = _format_list_services_result(tool_data)
                    return {
                        "turn_user_response": formatted["text"],
                        "matched_services": formatted["matched_services"],
                    }
                elif last_tool_name == "show_service_config":
                    return {"turn_user_response": _format_list_config_result(tool_data)}
                else:
                    # Generic formatting
                    return {"turn_user_response": json.dumps(tool_data, indent=2)}

            except (json.JSONDecodeError, TypeError):
                return {"turn_user_response": str(last_tool_result)}

    # FALLBACK: Legacy flow (reference_result from old pattern)
    reference_result = state.get("reference_result")
    tool_name = state.get("tool_name", "")
    remaining_params = list((state.get("remaining_parameters") or {}).keys())
    collected_params = state.get("collected_parameters") or {}

    # 1) If we have a result from tool execution, format and display it
    if reference_result:
        # Check for errors
        if "error" in reference_result:
            error_msg = reference_result.get("error", "Unknown error")
            return {
                "turn_user_response": f"Error: {error_msg}\n\nTry asking about:\n  - 'list services'\n  - 'show config of <service>'\n  - 'what's the cpu/memory of <service>'"
            }

        # Check for warnings (fuzzy match)
        if "warning" in reference_result:
            return {"turn_user_response": f"Warning: {reference_result['warning']}"}

        # Route to appropriate formatter based on tool
        result_tool = reference_result.get("tool", "")

        if result_tool == "list_services":
            formatted = _format_list_services_result(reference_result)
            # Return text response + matched_services for UI compatibility
            return {
                "turn_user_response": formatted["text"],
                "matched_services": formatted["matched_services"],
            }

        elif result_tool == "list_config_of_service":
            response = _format_list_config_result(reference_result)

        elif result_tool == "list_a_param_of_service":
            response = _format_list_param_result(reference_result)

        else:
            response = f"Error: Unknown tool: {result_tool}"

        return {"turn_user_response": response}

    # 2) If we have tool_name but no result, we're missing parameters
    if tool_name and remaining_params:
        # Build parameter list with human-readable labels
        param_labels = {
            "service_name": "Service Name",
            "parameter_name": "Parameter Name (cpu/memory/description)"
        }

        pieces = [f"<&h><&b>{param_labels.get(p, p)}</&b>" for p in remaining_params]

        msg = f"I need a few more details:\n"
        msg += "\n".join(pieces)

        # Show collected values so far
        if collected_params:
            collected_summary = "\n".join(f"  {k}: {v}" for k, v in collected_params.items())
            msg += f"\n\nCollected so far:\n{collected_summary}"

        return {"turn_user_response": msg}

    # 3) No tool specified
    return {
        "turn_user_response": "What would you like to know?\n\nTry:\n  - 'list services'\n  - 'show config of <service>'\n  - 'what's the cpu/memory of <service>'"
    }


# ============================================================================
# CREATE Response Formatters
# ============================================================================


def _get_allowed_values(param) -> Optional[List[str]]:
    """
    Extract allowed values from a parameter's value_source.

    Args:
        param: ParameterMeta object

    Returns:
        List of allowed values or None
    """
    if param.value_source and isinstance(param.value_source, StaticSource):
        return [opt.value for opt in param.value_source.options]
    return None


def _human_label(param_name: str) -> str:
    """
    Generate human-readable label for a parameter.
    Converts snake_case to Title Case.
    """
    return param_name.replace("_", " ").title()


def _get_available_resources_message(tenant_id: str) -> str:
    """
    Build a friendly message listing available resources for a tenant.

    Args:
        tenant_id: Tenant identifier

    Returns:
        Formatted message with available resources
    """
    available_resources = resource_meta_repo.list_for_tenant(TenantId(tenant_id))
    if available_resources:
        resource_names = [r.infra_display_name for r in available_resources.values()]
        return f"I can help you create resources like {', '.join(resource_names)}. Which one would you like to create?"
    return "What would you like to create?"


def _build_example_hints(param_names: List[str], param_definitions: Dict[str, Any]) -> List[str]:
    """
    Build example hints using param.examples or allowed_values.

    Only includes params that have meaningful examples.
    """
    examples = []
    for slot in param_names[:3]:  # Show up to 3 examples
        pdef = param_definitions.get(slot)
        if pdef:
            # First try allowed_values (ENUM params)
            allowed_values = _get_allowed_values(pdef)
            if allowed_values:
                examples.append(f"{pdef.name}: {allowed_values[0]}")
            # Then try param.examples (clean examples without arrows)
            elif pdef.examples:
                clean_examples = [ex for ex in pdef.examples if "→" not in str(ex)]
                if clean_examples:
                    examples.append(f"{pdef.name}: {clean_examples[0]}")
            # Skip params without meaningful examples
    return examples


def _handle_missing_placement_params(state: ChatState) -> Dict[str, Any]:
    """Handle response generation for missing placement parameters."""
    tenant_id = get_tenant_id(state)
    resource = state.get("turn_resource") or ""
    collected_placement = _sanitize_placement_parameters(
        state.get("collected_placement_parameters", {})
    )
    remaining_placement_keys = state.get("remaining_placement_parameters", {})

    # Get resource meta to build full parameter details
    resource_meta = resource_meta_repo.get(
        TenantId(tenant_id),
        InfraTypeCode(resource)
    )

    if not resource_meta:
        return {"turn_user_response": f"Sorry, I couldn't find configuration for resource."}

    # Get display name for user-friendly messages
    display_name = resource_meta.infra_display_name

    # Build full parameter details for missing placement params (for frontend)
    placement_params = {p.key: p for p in resource_meta.placement.parameters}
    remaining_placement_params = {}
    pieces = []  # For human-readable message

    for param_key in remaining_placement_keys.keys():
        param = placement_params.get(param_key)
        if param:
            # Build JSON structure for frontend
            param_info = {
                "name": param.name,
                "required": param.required,
                "type": param.type.value,
                "order": param.order
            }

            # Add value_source if present
            if param.value_source:
                if isinstance(param.value_source, StaticSource):
                    param_info["value_source"] = {
                        "type": "static",
                        "options": [{"label": opt.label, "value": opt.value} for opt in param.value_source.options]
                    }

            # Build human-readable display - just show parameter name
            pieces.append(f"<&h><&b>{param.name}</&b>")

            remaining_placement_params[param_key] = param_info

    # Build conversational message for user
    msg = f"Before we create the {display_name.upper()}, I need some placement details:\n"
    msg += "\n".join(pieces)

    # Show collected placement parameters so far (just names with markers, no values)
    if collected_placement:
        collected_lines = []
        for k, v in collected_placement.items():
            if v is not None and v != "None":
                param = placement_params.get(k)
                display_name_param = param.name if param else k
                collected_lines.append(f"<&h><&b>{display_name_param}</&b>")
        if collected_lines:
            msg += f"\n\nPlacement details collected so far:\n" + "\n".join(collected_lines)

    # Return both human-readable message AND structured data for frontend
    return {
        "turn_user_response": msg,
        "status": "missing_placement_parameters",
        "resource": resource,
        "cases": resource_meta.cases,  # Add cases from resource meta
        "collected_placement_parameters": collected_placement,
        "remaining_placement_parameters": remaining_placement_params
    }


def _handle_create_response(state: ChatState) -> Dict[str, str]:
    """Handle response generation for CREATE intent."""
    tenant_id = get_tenant_id(state)
    state_hint = state.get("state_hint", {})
    # Fall back to running_resource from state_hint if turn_resource is not set
    resource = state.get("turn_resource") or state_hint.get("running_resource") or ""

    slot_errors = state.get("slot_errors") or []
    remaining_params = list((state.get("remaining_parameters") or {}).keys())
    collected_params = state.get("collected_parameters") or {}
    running_phase = state_hint.get("running_phase", "parameter_collection")

    # Handle case where resource is None or empty
    if not resource:
        return {"turn_user_response": _get_available_resources_message(tenant_id)}

    # Get resource meta from new repo
    resource_meta = resource_meta_repo.get(
        TenantId(tenant_id),
        InfraTypeCode(resource)
    )

    if not resource_meta:
        return {"turn_user_response": f"Sorry, I couldn't find configuration for resource."}

    # Get display name for user-friendly messages
    display_name = resource_meta.infra_display_name

    # Only use attributes for parameter definitions (placement already in state)
    attributes = resource_meta.attributes.parameters
    param_definitions = {p.key: p for p in attributes}

    # 1) Validation error(s)
    if slot_errors:
        # Build error messages for all validation errors
        error_lines = []
        for error in slot_errors:
            slot = error.get("parameter", "")
            param_def = param_definitions.get(slot)
            error_msg = error["message"]
            if param_def:
                allowed_values = _get_allowed_values(param_def)
                if allowed_values:
                    error_msg += f" Choose one of: {allowed_values}."
            error_lines.append(error_msg)

        msg = "\n".join(error_lines)
        msg += "\nYou can copy parameter labels and provide values as you see fit."

        # Show collected values so far
        if collected_params:
            collected_lines = []
            for k, v in collected_params.items():
                pdef = param_definitions.get(k)
                display_name_param = pdef.name if pdef else k
                collected_lines.append(f"  <&h><&b>{display_name_param}</&b>: {v}")
            msg += f"\n\nCollected so far:\n" + "\n".join(collected_lines)

        return {
            "turn_user_response": msg,
            "has_prompted_for_params": True,  # Mark for next round feedback
            "cases": resource_meta.cases  # Add cases from resource meta
        }

    # 2) Missing mandatory params
    if remaining_params:
        # Build parameter list - show parameter name and allowed values for ENUMs
        pieces = []
        for slot in remaining_params:
            pdef = param_definitions.get(slot)
            if pdef:
                # Start with parameter name
                label = pdef.name

                # Check for conditional requirement reason
                conditional_reason = _get_conditional_reason(pdef, collected_params)
                if conditional_reason:
                    label = f"{label} ({conditional_reason})"

                # Show allowed values for ENUM parameters (no brackets)
                allowed_values = _get_allowed_values(pdef)
                if allowed_values:
                    pieces.append(f"<&h><&b>{label}</&b>: {', '.join(allowed_values)}")
                else:
                    pieces.append(f"<&h><&b>{label}</&b>")
            else:
                pieces.append(f"<&h><&b>{_human_label(slot)}</&b>")

        # Add prefix if no extraction on subsequent round
        extraction_feedback = _get_extraction_feedback(state)
        msg = extraction_feedback + "I need a few attributes for the " + display_name.upper() + ":\n"
        msg += "\n".join(pieces)

        # Show collected values so far (using human-readable names)
        if collected_params:
            collected_lines = []
            for k, v in collected_params.items():
                pdef = param_definitions.get(k)
                display_name_param = pdef.name if pdef else k
                collected_lines.append(f"  <&h><&b>{display_name_param}</&b>: {v}")
            msg += f"\n\nCollected so far:\n" + "\n".join(collected_lines)

        # Show required parameters with defaults (user can override)
        required_with_defaults = [
            p for p in attributes
            if p.required and p.default is not None and p.key not in collected_params
        ]
        if required_with_defaults:
            msg += "\n\nRequired parameters (using defaults, can override):"
            for param in required_with_defaults:
                msg += f"\n  <&h><&b>{param.name}</&b>: {param.default} (default)"

        # Show optional parameters (only show default if it has one)
        optional_params = [
            p for p in attributes
            if not p.required and p.key not in collected_params
        ]
        if optional_params:
            msg += "\n\nOptional parameters you can also provide:"
            for param in optional_params:
                if param.default is not None:
                    msg += f"\n  <&h><&b>{param.name}</&b>: {param.default} (default)"
                else:
                    msg += f"\n  <&h><&b>{param.name}</&b>"

        # Add example hints (only if we have examples)
        examples = _build_example_hints(remaining_params, param_definitions)
        if examples:
            msg += f"\n\nTip: You can provide values like `{', '.join(examples)}`"
        msg += "\nYou can copy parameter labels above and provide values as you see fit."

        return {
            "turn_user_response": msg,
            "has_prompted_for_params": True,  # Mark that we've prompted for params
            "cases": resource_meta.cases  # Add cases from resource meta
        }

    # 3) All parameters collected - show summary
    if collected_params:
        # Get extraction feedback if user sent a message that didn't extract anything
        extraction_feedback = _get_extraction_feedback(state)

        # Build complete parameter summary including optional parameters with defaults
        param_lines = []
        param_lines.append("Collected parameters:")

        # Show collected parameters
        for param in attributes:
            param_key = param.key
            param_name = param.name
            if param_key in collected_params:
                value = collected_params[param_key]
                param_lines.append(f"<&h><&b>{param_name}</&b>: {value}")
            elif not param.required and param.default is not None:
                # Optional parameter not provided - only show if it has a default
                param_lines.append(f"<&h><&b>{param_name}</&b>: {param.default} (default)")

        param_summary = "\n".join(param_lines)
        param_summary += "\n\nYou can update any value by copying the parameter label above, e.g. `Parameter Name: new value`"
        return {
            "turn_user_response": extraction_feedback + f"Ready to create {display_name.upper()}:\n{param_summary}",
            "has_prompted_for_params": True,  # Mark for next round feedback
            "cases": resource_meta.cases  # Add cases from resource meta
        }

    # 4) Default fallback
    return {"turn_user_response": _get_available_resources_message(tenant_id)}


def _handle_create_v2_response(state: ChatState) -> Dict[str, Any]:
    """
    Handle response generation for CREATE v2 (MCP tools flow).

    Extracts LLM response and structured data from MCP tool results.

    Args:
        state: Current chat state

    Returns:
        Dict with turn_user_response, attribute_parameters, placement_parameters, is_ready, cases
    """
    from langchain_core.messages import AIMessage, ToolMessage, RemoveMessage

    tenant_id = get_tenant_id(state)
    resource = state.get("turn_resource", "")

    messages = state.get("messages", [])
    logger.info(f"[CREATE_V2] Processing with {len(messages)} messages")

    # Debug: Log all message types
    for i, msg in enumerate(messages):
        msg_type = type(msg).__name__
        has_tool_calls = getattr(msg, "tool_calls", None)
        if isinstance(msg, ToolMessage):
            logger.info(f"[CREATE_V2] Message {i}: ToolMessage, name={getattr(msg, 'name', 'unknown')}, content_len={len(str(msg.content)) if msg.content else 0}")
        elif isinstance(msg, AIMessage):
            logger.info(f"[CREATE_V2] Message {i}: AIMessage, tool_calls={bool(has_tool_calls)}, content_len={len(msg.content) if msg.content else 0}")
        else:
            logger.info(f"[CREATE_V2] Message {i}: {msg_type}")

    if not messages:
        return {"turn_user_response": "No messages found in CREATE v2 flow"}

    # Strip orphaned AIMessages (tool_calls with no matching ToolMessage) from the
    # end of the message list. This happens when should_continue_tools_v2 short-circuits
    # the loop (is_ready=true guard) — the LLM's re-call AIMessage has no ToolMessage.
    # We collect their IDs and emit RemoveMessage objects so the add_messages reducer
    # actually deletes them from the checkpoint (it's append-only by default).
    scan_messages = list(messages)
    orphaned_ids = []
    while scan_messages and isinstance(scan_messages[-1], AIMessage) and getattr(scan_messages[-1], "tool_calls", None):
        tool_call_ids = {tc["id"] for tc in scan_messages[-1].tool_calls}
        has_responses = any(
            isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None) in tool_call_ids
            for m in scan_messages
        )
        if not has_responses:
            orphaned_msg = scan_messages.pop()
            orphaned_ids.append(orphaned_msg.id)
            logger.warning(f"[CREATE_V2] Removing orphaned AIMessage from checkpoint (ids={tool_call_ids}, msg_id={orphaned_msg.id})")
        else:
            break

    # Find the last AI message (final response from LLM) and last tool result
    last_ai_response = None
    last_tool_result = None
    last_tool_name = None

    # Search in reverse order to find both messages
    for msg in reversed(scan_messages):
        # AIMessage without tool_calls = final response
        if isinstance(msg, AIMessage) and msg.content and not getattr(msg, "tool_calls", None):
            if last_ai_response is None:  # Only take the first (last in sequence) one
                last_ai_response = msg.content
                logger.info(f"[CREATE_V2] Found final AI response (len={len(last_ai_response)})")

        # ToolMessage = tool execution result
        if isinstance(msg, ToolMessage) and last_tool_result is None:
            # Extract content from MCP tool result format
            # MCP tools return: list[TextContent] -> ToolMessage.content = [{"type": "text", "text": "..."}]
            content = msg.content
            if isinstance(content, list) and len(content) > 0:
                # MCP format: extract text from first TextContent item
                first_item = content[0]
                if isinstance(first_item, dict) and "text" in first_item:
                    last_tool_result = first_item["text"]
                else:
                    # Fallback: stringify the list
                    last_tool_result = str(content)
            else:
                # Not a list, use as-is
                last_tool_result = content

            last_tool_name = getattr(msg, "name", "")
            logger.info(f"[CREATE_V2] Found tool result: {last_tool_name}")

    logger.info(f"[CREATE_V2] Last tool: {last_tool_name}, has result: {bool(last_tool_result)}")

    # Build user-facing response: prefer LLM text, fallback to tool message field
    if last_ai_response:
        user_response = last_ai_response
    elif last_tool_result:
        # No LLM text response (e.g., is_ready guard short-circuited the loop).
        # Synthesize a response from the tool result's message field.
        try:
            tool_data_for_response = json.loads(last_tool_result)
            if tool_data_for_response.get("is_ready") is True:
                user_response = tool_data_for_response.get("message", "I've processed your request.")
            elif tool_data_for_response.get("status") == "error":
                user_response = tool_data_for_response.get("error", "An error occurred.")
            else:
                user_response = tool_data_for_response.get("message", "I've processed your request.")
        except (json.JSONDecodeError, TypeError):
            user_response = "I've processed your request."
    else:
        user_response = "I've processed your request."

    result: Dict[str, Any] = {
        "turn_user_response": user_response
    }

    # Extract structured data from CREATE tools (create_database, etc.)
    if last_tool_result:
        try:
            # last_tool_result is now a string (extracted from MCP format)
            tool_data = json.loads(last_tool_result)
            logger.info(f"[CREATE_V2] Tool data keys: {list(tool_data.keys())}")

            # Extract CREATE-specific structured data
            # Use the same key names that the API expects and save_conversation_turn reads
            if "attribute_parameters" in tool_data:
                result["collected_parameters"] = tool_data["attribute_parameters"]
                logger.info(f"[CREATE_V2] Extracted collected_parameters: {tool_data['attribute_parameters']}")
            if "placement_parameters" in tool_data:
                sanitized_placement = _sanitize_placement_parameters(
                    tool_data["placement_parameters"]
                )
                result["collected_placement_parameters"] = sanitized_placement
                logger.info(
                    "[CREATE_V2] Extracted collected_placement_parameters: "
                    f"{list(sanitized_placement.keys())}"
                )
            if "is_ready" in tool_data:
                result["is_ready"] = tool_data["is_ready"]
                logger.info(f"[CREATE_V2] Extracted is_ready: {tool_data['is_ready']}")
            if "queue_status" in tool_data:
                result["queue_status"] = tool_data["queue_status"]
                logger.info(f"[CREATE_V2] Extracted queue_status: {tool_data['queue_status']}")

        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"[CREATE_V2] Failed to parse tool result: {e}")

    # Get resource metadata to fetch cases (same pattern as traditional CREATE flow)
    if resource:
        try:
            resource_meta = resource_meta_repo.get(
                TenantId(tenant_id),
                InfraTypeCode(resource)
            )
            if resource_meta:
                result["cases"] = resource_meta.cases
                logger.info(f"[CREATE_V2] Added cases from resource meta: {resource_meta.cases}")
        except Exception as e:
            logger.error(f"[CREATE_V2] Failed to fetch resource meta for cases: {e}")

    # Emit RemoveMessage objects to clean orphaned AIMessages from the checkpoint.
    # This is critical: without this, the orphaned AIMessage(tool_calls) persists
    # in the checkpoint and causes a 400 error on the next turn (OpenAI requires
    # every AIMessage with tool_calls to have matching ToolMessage responses).
    if orphaned_ids:
        result["messages"] = [RemoveMessage(id=mid) for mid in orphaned_ids]
        logger.info(f"[CREATE_V2] Emitting {len(orphaned_ids)} RemoveMessage(s) to clean checkpoint")

    logger.info(f"[CREATE_V2] Result keys: {list(result.keys())}")
    return result


async def response_handler_node(state: ChatState, config) -> ChatState:
    """
    Generate user-facing messages for CREATE and REFERENCE workflows.

    Routes to appropriate handler based on turn_intent.

    Args:
        state: Current chat state
        config: Graph configuration containing thread_id

    Returns:
        Updated state with turn_user_response
    """
    # Extract thread_id from config
    thread_id = config.get("configurable", {}).get("thread_id", "unknown")

    # Special case: Switch confirmation - show message and stop
    pending_switch = state.get("pending_switch")
    switch_status = state.get("switch_intent_status", "")

    if pending_switch and switch_status == InfraChatConstants.SWITCH_INTENT_DETECTED:
        # This is a new switch confirmation request
        # Just return the confirmation message and route to END
        result = {
            "turn_user_response": state.get("turn_user_response", ""),
            "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH  # Reset status
        }
        # Save chat messages before returning
        await save_conversation_turn(state, result, thread_id)
        return result

    # Special case: Switch blocked (CREATE → CREATE) - show blocking message
    if switch_status == "switch_blocked":
        # Just return the blocking message and route to END
        result = {
            "turn_user_response": state.get("turn_user_response", ""),
            "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH  # Reset status
        }
        # Save chat messages before returning
        await save_conversation_turn(state, result, thread_id)
        return result

    # Special case: Workflow resuming - show resuming message and continue
    if switch_status == InfraChatConstants.SWITCH_INTENT_RESUMING:
        # This is a resume operation - just return the resuming message
        result = {
            "turn_user_response": state.get("turn_user_response", ""),
            "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH  # Reset status
        }
        # Save chat messages before returning
        await save_conversation_turn(state, result, thread_id)
        return result

    # Special case: Pre-validation failed - return error message to user
    logger.info(
        f"[RESPONSE_HANDLER] Pre-validation check: "
        f"turn_requires_user_input={state.get('turn_requires_user_input')}, "
        f"turn_user_response={state.get('turn_user_response')[:100] if state.get('turn_user_response') else None}"
    )
    if state.get("turn_requires_user_input") and state.get("turn_user_response"):
        logger.info("[RESPONSE_HANDLER] Returning pre-validation error message to user")
        result = {"turn_user_response": state.get("turn_user_response", "")}
        await save_conversation_turn(state, result, thread_id)
        return result

    intent = state.get("turn_intent", "")

    # Special case: Missing placement parameters - only for CREATE intent
    remaining_placement_params = state.get("remaining_placement_parameters", {})
    if intent == "CREATE" and remaining_placement_params:
        result = _handle_missing_placement_params(state)
        # Save chat messages before returning
        await save_conversation_turn(state, result, thread_id)
        return result

    # Route based on intent and generate response
    if intent == "REFERENCE":
        result = _handle_reference_response(state)
    elif intent == "CREATE":
        # Check if this is create_flow_v2 (has messages from MCP tools) or traditional CREATE flow
        messages = state.get("messages", [])
        if messages:
            # create_flow_v2: Handle MCP tool results with structured data extraction
            # Returns dict with: turn_user_response, collected_parameters, collected_placement_parameters, is_ready
            result = _handle_create_v2_response(state)
            logger.info(f"[RESPONSE_HANDLER] _handle_create_v2_response returned keys: {list(result.keys())}")
            logger.info(f"[RESPONSE_HANDLER] queue_status in result: {result.get('queue_status')}")
        else:
            # Traditional CREATE flow (v1)
            result = _handle_create_response(state)
    elif intent in ("QA", "UNSUPPORTED"):
        # Pass through - response already generated by execution nodes
        result = {"turn_user_response": state.get("turn_user_response", "")}
    else:
        result = {"turn_user_response": f"I'm not sure how to help with that. Intent: {intent}"}

    # Save chat messages before returning
    await save_conversation_turn(state, result, thread_id)

    # Log final result being returned to state
    logger.info(f"[RESPONSE_HANDLER] Returning result keys: {list(result.keys())}")
    if "collected_placement_parameters" in result:
        logger.info(f"[RESPONSE_HANDLER] collected_placement_parameters in result: {result['collected_placement_parameters']}")

    return result
