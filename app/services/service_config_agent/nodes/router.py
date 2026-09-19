"""
Router node for Service Config Agent.

Thin orchestrator: delegates routing to classifier and intent detection.
"""

import re
from typing import Dict, Any, Protocol, runtime_checkable, List, Optional

from app.services.service_config_agent.state import ServiceConfigAgentState


# Route categories
ROUTE_LEGACY = "legacy"
ROUTE_AGENTIC = "agentic"
ROUTE_SIMPLE_QA = "simple_qa"


@runtime_checkable
class RouteClassifierProtocol(Protocol):
    """Contract for route classification."""

    async def classify(
        self,
        message: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]] = None,
    ) -> str:
        """Classify message into route category."""
        ...


@runtime_checkable
class IntentDetectorProtocol(Protocol):
    """Contract for intent detection."""

    async def detect(
        self,
        message: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]] = None,
    ) -> str:
        """Detect intent from message."""
        ...


async def router_node(
    state: ServiceConfigAgentState,
    route_classifier: Optional[RouteClassifierProtocol] = None,
    intent_detector: Optional[IntentDetectorProtocol] = None,
) -> Dict[str, Any]:
    """
    Router node: thin orchestrator for route and intent classification.

    Workflow:
    1. Classify route (legacy/agentic/simple_qa)
    2. If legacy, detect specific intent
    3. Return route and intent

    Args:
        state: Current agent state
        route_classifier: Service for route classification (optional)
        intent_detector: Service for intent detection (for legacy routes)

    Returns:
        Updated state with route and intent set.
    """
    message = state["user_message"]
    history = state.get("history", [])

    # Build context for classification
    classify_context = {
        "is_new_session": state.get("is_new_session", False),
        "reference_service": state.get("reference_service_name"),
        "reference_has_config": state.get("reference_has_config", False),
    }

    # Determine route
    if route_classifier:
        route = await route_classifier.classify(message, classify_context, history)
    else:
        # Fallback to legacy for backward compatibility
        route = ROUTE_LEGACY

    # Detect intent based on route
    intent = ""
    updated_message = None

    if route == ROUTE_AGENTIC:
        intent = "agentic_query"

        # Check if this is a clarification confirmation (e.g., "yes" after "Did you mean X?")
        if message.lower().strip() in {"yes", "yep", "sure", "ok", "okay", "y"}:
            suggested_param = _extract_suggested_parameter(history)
            if suggested_param:
                # Transform "yes" to the suggested parameter for retry
                updated_message = suggested_param

        # Check if this is a value proposal follow-up (e.g., "can i choose 350")
        elif _is_value_proposal_message(message):
            param_from_offer = _extract_param_from_autofill_offer(history)
            proposed_value = _extract_proposed_value(message)
            if param_from_offer and proposed_value:
                # Transform to a validation query for the listener priority tool
                updated_message = f"is {proposed_value} okay for {param_from_offer}"

        # Check if this is a service selection after agentic ambiguity
        # (e.g., user selected "amal-serr" after compare query showed ambiguity)
        elif _has_pending_agentic_ambiguity(history):
            original_query = _extract_original_agentic_query(history)
            if original_query:
                # Combine original query with selected service
                # Replace ambiguous service reference with the selected one
                updated_message = _combine_query_with_service(original_query, message)

    elif route == ROUTE_SIMPLE_QA:
        # Simple QA (greetings, help) handled by legacy handler with help intent
        intent = "help"
    elif intent_detector:
        # LEGACY route - use intent detector
        intent = await intent_detector.detect(message, classify_context, history)

    result = {
        "route": route,
        "intent": intent,
    }

    # If we transformed the message, include it in state update
    if updated_message:
        result["user_message"] = updated_message

    return result


def _extract_suggested_parameter(history: List[Dict[str, str]]) -> Optional[str]:
    """
    Extract suggested parameter from clarification message in history.

    Looks for pattern: "Did you mean 'parameter_name'?"
    """
    if not history:
        return None

    # Find last agent message
    for msg in reversed(history):
        role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
        if role == "agent":
            message = msg.get('message', '') if isinstance(msg, dict) else getattr(msg, 'message', '')
            # Extract parameter from "Did you mean 'X'?" pattern
            match = re.search(r"Did you mean ['\"]?(\w+)['\"]?\?", message)
            if match:
                return match.group(1)
            break

    return None


def _is_value_proposal_message(message: str) -> bool:
    """
    Check if message proposes a specific value.

    Examples: "can i use 350", "how about 200", "is 50 okay"
    """
    value_patterns = [
        r"can\s+i\s+(use|choose|set|pick)\s+\d+",
        r"(how|what)\s+about\s+\d+",
        r"is\s+\d+\s+(ok|okay|good|valid|available)",
        r"^use\s+\d+",
        r"^set\s+(it\s+)?to\s+\d+",
    ]
    for pattern in value_patterns:
        if re.search(pattern, message.lower(), re.IGNORECASE):
            return True
    return False


def _extract_param_from_autofill_offer(history: List[Dict[str, str]]) -> Optional[str]:
    """
    Extract parameter name from autofill offer in history.

    Looks for pattern: "Would you like me to fill 'parameter_name' with value"
    """
    if not history:
        return None

    # Check last few agent messages
    agent_count = 0
    for msg in reversed(history):
        role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
        if role == "agent":
            agent_count += 1
            if agent_count > 2:
                break
            message = msg.get('message', '') if isinstance(msg, dict) else getattr(msg, 'message', '')
            # Extract parameter from "Would you like me to fill 'X'" pattern
            match = re.search(r"Would you like me to fill ['\"]?(\w+)['\"]?", message)
            if match:
                return match.group(1)

    return None


def _extract_proposed_value(message: str) -> Optional[str]:
    """
    Extract the proposed numeric value from user message.

    Examples:
        "can i use 350" -> "350"
        "how about 200" -> "200"
        "is 50 okay" -> "50"
    """
    match = re.search(r"\d+", message)
    if match:
        return match.group(0)
    return None


def _has_pending_agentic_ambiguity(history: List[Dict[str, str]]) -> bool:
    """
    Check if there's a pending agentic service ambiguity in history.
    """
    if not history:
        return False

    for msg in reversed(history):
        role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
        if role == "agent":
            message = msg.get('message', '') if isinstance(msg, dict) else getattr(msg, 'message', '')
            if "I found multiple services matching your query" in message:
                return True
            break

    return False


def _extract_original_agentic_query(history: List[Dict[str, str]]) -> Optional[str]:
    """
    Extract the original user query that triggered service ambiguity.

    Looks for the user message before the ambiguity response.
    """
    if not history:
        return None

    # Find the ambiguity message, then get the user message before it
    found_ambiguity = False
    for msg in reversed(history):
        role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
        message = msg.get('message', '') if isinstance(msg, dict) else getattr(msg, 'message', '')

        if role == "agent" and "I found multiple services matching your query" in message:
            found_ambiguity = True
            continue

        if found_ambiguity and role == "user":
            return message

    return None


def _combine_query_with_service(original_query: str, selected_service: str) -> str:
    """
    Combine original query with the selected service name.

    Handles patterns like:
    - "compare dev and staging values of ajmal service" + "amal-serr"
      -> "compare dev and staging values of amal-serr"
    """
    # Common patterns to replace
    patterns = [
        # "X service" pattern
        (r'\b\w+\s+service\b', selected_service),
        # "of X" pattern at end
        (r'\bof\s+\w+\s*$', f'of {selected_service}'),
        # "for X" pattern at end
        (r'\bfor\s+\w+\s*$', f'for {selected_service}'),
    ]

    result = original_query
    for pattern, replacement in patterns:
        new_result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
        if new_result != result:
            return new_result

    # If no pattern matched, append "for <service>"
    return f"{original_query} for {selected_service}"
