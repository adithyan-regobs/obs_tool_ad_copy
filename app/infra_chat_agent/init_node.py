import logging
from app.infra_chat_agent.chat_state import ChatState

logger = logging.getLogger(__name__)

def init_node(state: ChatState) -> ChatState:
    # Ensure defaults to avoid nulls in first turn.
    # Clear any previous stale response to keep history clean.
    # Clear any turn objects to keep it clean.
    # WARNING - should not clear any state data persisted across turns.

    # Debug logging to see what state is received
    collected = state.get("collected_parameters", {})
    state_hint = state.get("state_hint", {})
    logger.info(f"[INIT_NODE] User message: {state.get('user_message', 'N/A')[:50]}")
    logger.info(f"[INIT_NODE] Collected parameters from state: {collected}")
    logger.info(f"[INIT_NODE] State hint: {state_hint}")

    return {
        "turn_intent" : None,
        "turn_resource" : None,
        "slot_parameters": {},
        "slot_errors": [], #TODO: it is not cleared in poc. verify why after implementation.

        "turn_user_response": None, #Clear stale response from previous turn.
        "turn_requires_user_input": False,  # Clear pre-validation flag from previous turn
        "cases": None,  # Clear cases from previous turn (will be set fresh by policy_validator)

        "is_ready": False,  # Initialize to False at every turn
        "guardrail_status": "",
        "guardrail_reason": None,
        "validated_user_message": None,

        "session_state_history": state.get("session_state_history", {}), #Default in first turn.
        "state_hint": state.get("state_hint", {}), #Default in first turn.
        "collected_parameters": state.get("collected_parameters", {}),
        "confirmed_parameters": {},  # Clear confirmed params from previous turn
        "remaining_parameters": state.get("remaining_parameters", {}),

        # Initialize placement parameter tracking
        "collected_placement_parameters": state.get("collected_placement_parameters", {}),
        "remaining_placement_parameters": state.get("remaining_placement_parameters", {}),
        "validate_params_state": state.get(
            "validate_params_state",
            {"tool_name": None, "valid": {}}
        ),

        "parameter_extraction_method": "",
        "intent_detection_method_status": "",
        "intent_detection_llm_prechecks_status":"",

        # Clear REFERENCE workflow turn-specific state
        "remaining_reference_parameters": {},  # Clear from previous turn (options sent to frontend)
        # Note: messages is preserved via LangGraph checkpoint for conversation history

        # Preserve switch-related state across turns (only if present)
        "pending_switch": state.get("pending_switch") if state.get("pending_switch") else None,
        "switch_intent_status": state.get("switch_intent_status", ""),
        "switch_counter": state.get("switch_counter", 0),
        "tool_name": state.get("tool_name", ""),  # Preserve tool_name for REFERENCE workflows
        "switch_matches": state.get("switch_matches", []),  # For disambiguation UI
        "switch_awaiting_selection": state.get("switch_awaiting_selection", False),  # For selection flow
        "has_prompted_for_params": state.get("has_prompted_for_params", False)  # For extraction feedback
    }
