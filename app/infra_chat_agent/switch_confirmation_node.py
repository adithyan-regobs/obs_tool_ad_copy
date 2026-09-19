"""
Switch confirmation node for handling user responses to switch confirmation.

Detects yes/no responses and updates session_state_history accordingly.
"""
from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.infra_chat_constants import InfraChatConstants


def switch_confirmation_node(state: ChatState) -> ChatState:
    """
    Handle user response to switch confirmation.

    If user confirms (yes/ok/go ahead):
    - Mark pending session as "active"
    - Clear current state_hint
    - Start new workflow

    If user rejects (no/cancel):
    - Mark pending session as "rejected"
    - Keep current state_hint
    - Continue with current workflow

    Args:
        state: Current chat state with pending_switch

    Returns:
        Updated state with cleared pending_switch and updated session status
    """
    pending_switch = state.get("pending_switch")
    if not pending_switch:
        # No pending switch, should not happen
        return {"switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH}

    user_message = (state.get("user_message", "") or "").lower().strip()
    session_key = pending_switch.get("session_key")

    # Detect yes/no response using word-based matching (not substring)
    user_words = set(user_message.split())
    yes_keywords = {"yes", "y", "ok", "okay", "sure", "confirm", "switch"}
    no_keywords = {"no", "n", "cancel", "dont"}
    # Multi-word phrases need substring check
    yes_phrases = ["go ahead"]
    no_phrases = ["don't"]

    is_yes = bool(user_words & yes_keywords) or any(phrase in user_message for phrase in yes_phrases)
    is_no = bool(user_words & no_keywords) or any(phrase in user_message for phrase in no_phrases)

    session_history = state.get("session_state_history") or {}

    if is_yes:
        # User confirmed the switch
        # Mark session as "active"
        if session_key and session_key in session_history:
            session_history[session_key]["session_status"] = "active"

        # Get new intent/resource from pending_switch
        new_intent = pending_switch.get("new_intent", "")
        new_resource = pending_switch.get("new_resource", "")
        new_tool_name = pending_switch.get("new_tool_name", "")

        # Clear current workflow state
        # Build new state_hint for the new workflow
        new_state_hint = {}
        if new_intent == "REFERENCE" and new_tool_name:
            new_state_hint = {
                "running_intent": "REFERENCE",
                "running_resource": new_resource if new_resource else "",
                "running_phase": "parameter_collection",
                "tool_name": new_tool_name
            }
        elif new_intent == "CREATE" and new_resource:
            new_state_hint = {
                "running_intent": "CREATE",
                "running_resource": new_resource,
                "running_phase": "parameter_collection"
            }

        return {
            "pending_switch": {},  # Clear pending_switch
            "session_state_history": session_history,
            "state_hint": new_state_hint,
            "collected_parameters": {},  # Clear for new workflow
            "remaining_parameters": {},  # Clear for new workflow
            "tool_name": new_tool_name,  # Set tool_name for REFERENCE
            "has_prompted_for_params": False,  # Reset for new workflow
            "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH
        }

    elif is_no:
        # User rejected the switch
        # Mark session as "rejected"
        if session_key and session_key in session_history:
            session_history[session_key]["session_status"] = "rejected"

        # Keep current state_hint (don't switch)
        # Clear pending_switch
        # Prompt user again for missing parameters
        current_state_hint = state.get("state_hint") or {}
        running_intent = current_state_hint.get("running_intent", "")

        msg = "Okay, continuing with "
        if running_intent == "CREATE":
            resource = current_state_hint.get("running_resource", "")
            msg += f"creating {resource.upper()}."
        elif running_intent == "REFERENCE":
            tool_name = current_state_hint.get("tool_name", "")
            msg += f"{tool_name.replace('_', ' ')}."
        else:
            msg += "your current task."

        return {
            "pending_switch": {},  # Clear pending_switch
            "session_state_history": session_history,
            "turn_user_response": msg,
            "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH
        }

    else:
        # Could not detect yes/no, ask again
        from_intent = pending_switch.get("from_intent", "")
        from_resource = pending_switch.get("from_resource", "")
        from_tool_name = pending_switch.get("from_tool_name", "")

        msg = "Please confirm: "
        if from_intent == "CREATE":
            msg += f"Switch from creating {from_resource.upper()}? (yes/no)"
        else:  # REFERENCE
            msg += f"Switch from {from_tool_name.replace('_', ' ')}? (yes/no)"

        return {
            "turn_user_response": msg,
            "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH
        }
