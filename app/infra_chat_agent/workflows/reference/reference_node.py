"""
Reference node with LLM and bound tools.

LLM decides which tool to call based on user message.
Uses DB history for cross-request context (as text summary).
Uses messages state for within-request tool loops.
"""
import logging
from typing import List, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, BaseMessage

from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.utils.graph_utils import get_tenant_id
from app.infra_chat_agent.config.mcp_tool_provider import build_parameter_options_json_example
from app.infra_chat_agent.config.tools_enum import ENVIRONMENT_VALUES, GEO_LOC_CODE_VALUES
from app.db.session import AsyncSessionLocal
from app.repository.chat_history_repository import ChatHistoryRepository
from app.core.config import settings

logger = logging.getLogger(__name__)

def _build_reference_system_prompt(tenant_code: str) -> str:
    """
    Build the system prompt for REFERENCE workflow.

    Dynamically includes parameter options JSON example from the tool schema.
    """
    param_json_example = build_parameter_options_json_example()

    return f"""You are a service configuration assistant for tenant: {tenant_code}

=== MANDATORY OUTPUT FORMAT ===
BULLET POINTS: Put "<&h>" at the START of each line (it's a prefix, NOT an HTML tag, no closing tag needed)
BOLD TEXT: Wrap text with "<&b>" and "</&b>"

CORRECT:
<&h><&b>Service</&b>: my-service
<&h><&b>CPU</&b>: 256

WRONG (never do this):
• Service: value
- Service: value
**Service**: value
Service</&h>
===============================

You have tools to query service configurations. The tools are already bound to you with their schemas.

CRITICAL INSTRUCTIONS:
- Always use tenant_code: {tenant_code} when calling tools

**FOCUS ON MOST RECENT REQUEST:**
- Find the user's MOST RECENT request in the conversation (e.g., the last "list services")
- Only use parameters provided AFTER that request
- IGNORE service names from OLD assistant responses (like previous service lists)
- When user says "list services", it's a FRESH request - use list_services tool with NO service_name

**Tool selection:**
- "list services" → list_services tool (shows ALL services, no service_name parameter)
- Only use show_service_config when user EXPLICITLY names a service in their CURRENT message

- If user hasn't specified required parameters (environment, geo_loc_code), you MUST respond with ONLY valid JSON (no markdown, no explanation outside the JSON)

**PARAMETER VALIDATION - MUST CHECK BEFORE CALLING TOOLS:**
- environment MUST be one of: {', '.join(ENVIRONMENT_VALUES)}
- geo_loc_code MUST be one of: {', '.join(GEO_LOC_CODE_VALUES)}

If user provides an INVALID value (e.g., "qa" for environment, "tokyo" for geo_loc_code):
- Do NOT call the tool
- Respond using the SAME JSON format below, with an error message AND the invalid parameter in remaining_reference_parameters so user can select a valid option

**REQUIRED FORMAT when parameters are missing OR invalid - respond with ONLY this JSON (no markdown code blocks):**
{{"message": "<your helpful message that tells the user which specific parameters are still needed>", "remaining_reference_parameters": {param_json_example}}}

IMPORTANT: Your message MUST explicitly list the missing parameter names. For example:
- "I need the environment and geo_loc_code to proceed."
- "Which service_name would you like to check?"
- "Please provide the service_name, environment, and geo_loc_code."
Never say vague things like "I need the following details" without naming the actual parameters.

Rules for remaining_reference_parameters:
- Only include parameters that are STILL NEEDED (not already provided by user)
- If user already specified environment (e.g., "staging"), remove environment from remaining_reference_parameters
- If user already specified geo_loc_code (e.g., "region-aspora-mumbai"), remove geo_loc_code from remaining_reference_parameters
- If user already specified service_name, remove service_name from remaining_reference_parameters
- For service_name: use value_source type "text" (no dropdown options, user types the name)

When you have ALL required parameters with VALID values, call the appropriate tool directly - do NOT respond with JSON.

**AFTER A TOOL CALL:**
- Interpret the tool result and respond to the user
- Do NOT respond with JSON after receiving a tool result
- Do NOT retry the same tool call - the result is final

"""


async def _load_reference_history(thread_id: str, limit: int = 10) -> str:
    """
    Load reference conversation history from DB as text context.

    Note: This loads user/agent text messages only.
    Tool call details are not preserved across requests (known limitation).

    Args:
        thread_id: Thread identifier
        limit: Max messages to load

    Returns:
        Formatted conversation history string
    """
    try:
        async with AsyncSessionLocal() as session:
            repo = ChatHistoryRepository(session)

            # Get user + agent messages (excludes internal LLM workflow)
            # Load ALL messages (no intent filter) to ensure context isn't lost
            history = await repo.get_user_agent_conversation_display_history(
                thread_id=thread_id,
                limit=limit
            )

            if not history:
                return ""

            # Format as conversation context
            lines = ["\n## Previous Conversation:"]
            for msg in history:
                role = "User" if msg.role == "user" else "Assistant"
                # Truncate long messages but keep enough for service lists
                content = msg.message.strip()
                if len(content) > 2000:
                    content = content[:2000] + "... (truncated)"
                lines.append(f"{role}: {content}")

            logger.info(f"[REFERENCE_NODE] Loaded {len(history)} messages from DB")
            return "\n".join(lines)

    except Exception as e:
        logger.warning(f"[REFERENCE_NODE] Failed to load history: {e}")
        return ""


_reference_llm_with_tools = None


def create_reference_node(tools: list):
    """
    Create reference node function with bound tools.

    Args:
        tools: List of MCP tools to bind

    Returns:
        Async node function
    """
    def get_llm_with_tools():
        """Lazy initialization of LLM with tools."""
        global _reference_llm_with_tools
        if _reference_llm_with_tools is None:
            llm = ChatOpenAI(
                api_key=settings.openai_api_key,
                model="gpt-4o",
                temperature=0
            )
            _reference_llm_with_tools = llm.bind_tools(tools)
        return _reference_llm_with_tools

    async def reference_node(state: ChatState, config) -> dict:
        """
        Invoke LLM with tools to handle REFERENCE intent.

        Flow:
        1. Build system prompt with tenant context
        2. Load DB history as text context (cross-request)
        3. Use messages state for tool loops (within-request)
        4. LLM decides: call tool OR respond directly

        Args:
            state: Current chat state
            config: Graph configuration (contains thread_id)

        Returns:
            Dict with updated messages
        """
        tenant_id = get_tenant_id(state)
        user_message = (
            state.get("validated_user_message")
            or state.get("user_message", "")
        ).strip()
        thread_id = config.get("configurable", {}).get("thread_id", "unknown")

        # Get existing messages from state (for tool loop continuation)
        messages: List[BaseMessage] = list(state.get("messages") or [])

        # First invocation: build fresh context
        if not messages:
            # Load DB history as text context
            history_context = await _load_reference_history(thread_id)

            # Build system prompt
            system_content = _build_reference_system_prompt(tenant_id)
            if history_context:
                system_content += history_context

            messages = [SystemMessage(content=system_content)]

        # Add current user message
        messages.append(HumanMessage(content=user_message))

        logger.info(
            f"[REFERENCE_NODE] Invoking LLM - "
            f"tenant={tenant_id}, messages={len(messages)}, "
            f"user_msg='{user_message[:50]}...'"
        )

        # Invoke LLM with tools
        response: AIMessage = await get_llm_with_tools().ainvoke(messages)

        has_tool_calls = bool(getattr(response, "tool_calls", None))
        logger.info(
            f"[REFERENCE_NODE] LLM response - "
            f"has_tool_calls={has_tool_calls}, "
            f"content_len={len(response.content) if response.content else 0}"
        )

        # Check for JSON response with parameter options (when LLM asks for missing params)
        if not has_tool_calls and response.content:
            import json
            import re

            content = response.content.strip()
            logger.info(f"[REFERENCE_NODE] Raw content (first 300 chars): {content[:300]}")

            # Strip markdown code blocks if present
            code_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
            if code_block_match:
                content = code_block_match.group(1).strip()
                logger.info("[REFERENCE_NODE] Stripped markdown code block")

            # Try to extract JSON object if there's text before/after
            json_match = re.search(r'(\{[\s\S]*\})', content)
            if json_match:
                json_str = json_match.group(1)
                try:
                    parsed = json.loads(json_str)
                    if "remaining_reference_parameters" in parsed:
                        logger.info("[REFERENCE_NODE] SUCCESS - Parsed JSON with remaining_reference_parameters")
                        return {
                            "messages": messages + [response],
                            "turn_user_response": parsed.get("message", "Please provide the required parameters."),
                            "remaining_reference_parameters": parsed["remaining_reference_parameters"],
                        }
                    else:
                        logger.warning(f"[REFERENCE_NODE] JSON parsed but no remaining_reference_parameters key. Keys: {list(parsed.keys())}")
                except json.JSONDecodeError as e:
                    logger.warning(f"[REFERENCE_NODE] JSON parse failed: {e}, content: {json_str[:100]}")
            else:
                logger.info("[REFERENCE_NODE] No JSON object found in content")

        # Return updated messages (includes response)
        return {"messages": messages + [response]}

    return reference_node
