import time
from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.infra_chat_constants import InfraChatConstants
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.infra_chat_agent.config.config_models import TenantId, InfraTypeCode
from app.infra_chat_agent.utils.graph_utils import get_tenant_id


def _get_resource_display_name(tenant_id: str, infra_type_code: str) -> str:
    """Get human-readable display name for a resource type."""
    if not infra_type_code:
        return infra_type_code
    resource_meta = resource_meta_repo.get(
        TenantId(tenant_id),
        InfraTypeCode(infra_type_code)
    )
    if resource_meta:
        return resource_meta.infra_display_name
    return infra_type_code


def switch_intent_node(state: ChatState) -> ChatState:
    """
    Intent switching node.

    Handles scenarios where users want to switch between different intents
    or resources mid-conversation (e.g., changing from CREATE to REFERENCE).

    Sets state_hint to track active workflow context for next turns.

    Also detects intent switches and prompts user for confirmation before
    switching from incomplete workflows.
    """
    import logging
    logger = logging.getLogger(__name__)

    intent = state.get("turn_intent", "")
    resource = state.get("turn_resource", "")
    pending_switch = state.get("pending_switch")
    clear_pending_switch = False  # Track if we need to clear pending_switch from state
    tenant_id = get_tenant_id(state)

    # Debug logging
    logger.info(f"[SWITCH_INTENT] Called with intent={intent}, resource={resource}")
    logger.info(f"[SWITCH_INTENT] pending_switch={pending_switch}")

    # IMPORTANT: QA and UNSUPPORTED are stateless - bypass all switch logic
    # User can ask questions or get unsupported responses without interrupting workflow
    if intent in ("QA", "UNSUPPORTED"):
        logger.info(f"[SWITCH_INTENT] {intent} intent detected - bypassing switch logic")
        return {
            "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH
        }

    # If there's a pending_switch, check if user is responding with yes/no
    # or providing a new intent
    if pending_switch:
        user_message = (state.get("user_message", "") or "").lower().strip()
        # Use word-based matching (not substring) to avoid false positives like "roll no"
        user_words = set(user_message.split())
        yes_keywords = {"yes", "y", "ok", "okay", "sure", "confirm", "switch"}
        no_keywords = {"no", "n", "cancel", "dont"}
        # Multi-word phrases need substring check
        yes_phrases = ["go ahead"]
        no_phrases = ["don't"]

        is_yes_no = (
            bool(user_words & yes_keywords) or
            bool(user_words & no_keywords) or
            any(phrase in user_message for phrase in yes_phrases + no_phrases)
        )

        if not is_yes_no:
            # User provided a new intent instead of answering yes/no
            # Mark pending_switch as rejected and clear it
            session_history = state.get("session_state_history") or {}
            session_key = pending_switch.get("session_key")

            if session_key and session_key in session_history:
                session_history[session_key]["session_status"] = "rejected"

            # Clear pending_switch and continue with new intent
            # Fall through to normal processing below
            pending_switch = None
            clear_pending_switch = True  # Signal to clear from state

    # Get current state_hint to see if there's a running workflow
    state_hint = state.get("state_hint") or {}
    running_intent = state_hint.get("running_intent", "")
    running_resource = state_hint.get("running_resource", "")
    running_tool_name = state_hint.get("tool_name", "")

    # Get tool_name from slot_parameters (set by intent_detector for REFERENCE)
    slot_params = state.get("slot_parameters") or {}
    extracted_tool_name = slot_params.get("tool_name", "")

    # IMPORTANT: For REFERENCE workflows, preserve tool_name from state if LLM didn't extract it
    # This prevents false switch detection when user provides parameter values
    # Only preserve if extracted_tool_name is empty (LLM didn't extract a new tool_name)
    if not extracted_tool_name:
        # Check state.tool_name first (set by switch_intent_node in previous turn)
        # Then check state_hint.tool_name (set during workflow initialization)
        state_tool_name = state.get("tool_name", "")
        if state_tool_name:
            logger.info(f"[SWITCH_INTENT] Preserving tool_name from state.tool_name: {state_tool_name}")
            tool_name = state_tool_name
        elif running_tool_name:
            logger.info(f"[SWITCH_INTENT] Preserving tool_name from state_hint.tool_name: {running_tool_name}")
            tool_name = running_tool_name
        else:
            tool_name = ""
    else:
        # LLM extracted a tool_name (legitimate switch or new workflow)
        tool_name = extracted_tool_name

    # Debug logging
    logger.info(f"[SWITCH_INTENT] state_hint={state_hint}")
    logger.info(f"[SWITCH_INTENT] running_intent={running_intent}, running_resource={running_resource}")

    # Special case: If pending_switch exists and detected intent/tool matches the NEW intent/tool,
    # this means the LLM correctly processed a yes/no response. Clear pending_switch and proceed.
    if pending_switch:
        new_intent = pending_switch.get("new_intent")
        new_resource = pending_switch.get("new_resource")
        new_tool_name = pending_switch.get("new_tool_name")
        session_key = pending_switch.get("session_key")

        if (intent == new_intent and
            resource == new_resource and
            tool_name == new_tool_name):
            # LLM correctly confirmed the switch - clear pending_switch, mark session as active, proceed
            logger.info(f"[SWITCH_INTENT] Detected intent matches pending_switch NEW intent. Clearing pending_switch.")

            # Mark the pending session as active
            session_history = state.get("session_state_history") or {}
            if session_key and session_key in session_history:
                session_history[session_key]["session_status"] = "active"

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
                "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH,
                "tool_name": new_tool_name,  # Set tool_name for REFERENCE
                "collected_parameters": {},  # Clear old workflow's collected params
                "remaining_parameters": {},  # Clear old workflow's remaining params
                "validate_params_state": {"tool_name": None, "valid": {}},
                "has_prompted_for_params": False  # Reset for new workflow
            }

    # Detect if user is trying to switch from an incomplete workflow
    is_switch = False  # Default: not a switch
    if running_intent:
        # Normalize resource values: treat None and empty string as equal
        # For REFERENCE workflows, resource is often None/empty and shouldn't trigger switch
        normalized_resource = resource or ""
        normalized_running_resource = running_resource or ""

        # Check if this is a switch (intent/resource/tool changed)
        is_switch = (
            intent != running_intent or
            normalized_resource != normalized_running_resource or
            tool_name != running_tool_name
        )

        logger.info(f"[SWITCH_INTENT] Calculated is_switch={is_switch} (intent={intent} vs {running_intent}, resource={resource} vs {running_resource}, tool={tool_name} vs {running_tool_name})")

        # IMPORTANT: If detected intent/tool_name matches running workflow exactly, this is NOT a switch
        # User is just providing parameters for the current workflow
        if not is_switch and running_intent:
            logger.info(f"[SWITCH_INTENT] Detected intent/tool matches running workflow - continuing parameter collection")
            return {
                "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH
            }

        # NEW: Check if this is a RESUME operation (user switching to a suspended workflow)
        # For CREATE: Direct resume (unambiguous - only one CREATE per resource)
        # For REFERENCE: Check for matches (may need disambiguation)
        # IMPORTANT: Check this BEFORE normal switch logic, even if is_switch=False
        session_history = state.get("session_state_history") or {}
        if (intent == "CREATE" or intent == "REFERENCE") and not state.get("switch_awaiting_selection"):
            logger.info(f"[SWITCH_INTENT] Checking for resume candidates: intent={intent}, resource={resource}, tool={tool_name}")

            # Find matching sessions in session_history
            matches = []
            for session_key, session_state in session_history.items():
                # Check for both "active" and "pending_confirmation" statuses
                # "pending_confirmation" means it was suspended during a switch request
                session_status = session_state.get("session_status")
                if session_status not in ["active", "pending_confirmation"]:
                    continue

                session_intent = session_state.get("session_intent")
                session_resource = session_state.get("session_resource")
                session_tool = session_state.get("session_tool_name", "")

                # For CREATE: match by resource
                if intent == "CREATE" and session_intent == "CREATE":
                    if resource and resource == session_resource:
                        matches.append((session_key, session_state))
                        logger.info(f"[SWITCH_INTENT] Found CREATE match: {session_key}")

                # For REFERENCE: match by tool_name (exact match)
                elif intent == "REFERENCE" and session_intent == "REFERENCE":
                    if tool_name and tool_name == session_tool:
                        matches.append((session_key, session_state))
                        logger.info(f"[SWITCH_INTENT] Found REFERENCE match: {session_key}")

            # Handle matches
            if matches:
                logger.info(f"[SWITCH_INTENT] Found {len(matches)} resume candidates")

                # For CREATE: Unambiguous - restore directly
                if intent == "CREATE" and len(matches) == 1:
                    session_key, session_state = matches[0]
                    logger.info(f"[SWITCH_INTENT] Resuming CREATE workflow: {session_key}")

                    restored_state_hint = {
                        "running_intent": session_state["session_intent"],
                        "running_resource": session_state["session_resource"],
                        "running_phase": session_state["session_phase"],
                        "tool_name": session_state.get("session_tool_name", "")
                    }

                    return {
                        "state_hint": restored_state_hint,
                        "collected_parameters": session_state["session_collected_parameters"],
                        "remaining_parameters": session_state["session_remaining_parameters"],
                        "validate_params_state": {"tool_name": None, "valid": {}},
                        "session_state_history": session_history,
                        "switch_intent_status": InfraChatConstants.SWITCH_INTENT_RESUMING,  # Signal that we're resuming
                        "tool_name": session_state.get("session_tool_name", ""),
                        "turn_user_response": f"Resuming {session_state['session_intent'].lower()} {session_state['session_resource'].upper()} workflow..."
                    }

                # For REFERENCE: Check ambiguity
                elif intent == "REFERENCE":
                    if len(matches) == 1:
                        # Unambiguous - restore directly
                        session_key, session_state = matches[0]
                        logger.info(f"[SWITCH_INTENT] Resuming REFERENCE workflow: {session_key}")

                        restored_state_hint = {
                            "running_intent": session_state["session_intent"],
                            "running_resource": session_state["session_resource"],
                            "running_phase": session_state["session_phase"],
                            "tool_name": session_state["session_tool_name"]
                        }

                        return {
                            "state_hint": restored_state_hint,
                            "collected_parameters": session_state["session_collected_parameters"],
                            "remaining_parameters": session_state["session_remaining_parameters"],
                            "validate_params_state": {"tool_name": None, "valid": {}},
                            "session_state_history": session_history,
                            "switch_intent_status": InfraChatConstants.SWITCH_INTENT_RESUMING,  # Signal that we're resuming
                            "tool_name": session_state["session_tool_name"],
                            "turn_user_response": f"Resuming {session_state['session_tool_name']} workflow..."
                        }
                    else:
                        # Ambiguous - present disambiguation UI
                        logger.info(f"[SWITCH_INTENT] Ambiguous REFERENCE resume: {len(matches)} matches")

                        ui = "I found multiple incomplete workflows:\n\n"
                        for i, (_, sess) in enumerate(matches, 1):
                            tool = sess.get("session_tool_name", "")
                            resource = sess.get("session_resource", "")
                            collected = sess.get("session_collected_parameters", {})
                            remaining = sess.get("session_remaining_parameters", {})

                            # Build display line
                            ui += f"{i}. {tool}"

                            # Show collected parameters
                            if collected:
                                params_str = ", ".join([f"{k}={v}" for k, v in collected.items()])
                                ui += f" (collected: {params_str})"
                            elif remaining:
                                # Show what's remaining if nothing collected
                                remaining_str = ", ".join(remaining.keys())
                                ui += f" (need: {remaining_str})"
                            else:
                                ui += f" (no parameters collected yet)"

                            ui += "\n"

                        ui += "\nWhich one would you like to resume?\n"
                        ui += "- Say the number (1, 2, 3...)\n"
                        ui += "- Or say 'new' to start a fresh workflow"

                        return {
                            "turn_user_response": ui,
                            "switch_matches": matches,
                            "switch_awaiting_selection": True,
                            "switch_intent_status": "switch_disambiguation"  # New status
                        }

        # Check if user is responding to disambiguation UI
        if state.get("switch_awaiting_selection"):
            user_input = (state.get("user_message", "") or "").lower().strip()
            matches = state.get("switch_matches", [])

            logger.info(f"[SWITCH_INTENT] Processing switch selection: user_input='{user_input}', matches={len(matches)}")

            # Check if user wants to start new
            if user_input in ["new", "create new", "start new", "fresh"]:
                logger.info(f"[SWITCH_INTENT] User wants to start new workflow")
                # Clear switch state and proceed as new workflow (fall through to normal logic)
                return {
                    "switch_awaiting_selection": False,
                    "switch_matches": [],
                    "validate_params_state": {"tool_name": None, "valid": {}},
                    "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH
                }

            # Try to parse as number
            try:
                index = int(user_input) - 1
                if 0 <= index < len(matches):
                    session_key, session_state = matches[index]
                    logger.info(f"[SWITCH_INTENT] User selected match #{index+1}: {session_key}")

                    restored_state_hint = {
                        "running_intent": session_state["session_intent"],
                        "running_resource": session_state["session_resource"],
                        "running_phase": session_state["session_phase"],
                        "tool_name": session_state["session_tool_name"]
                    }

                    return {
                        "state_hint": restored_state_hint,
                        "collected_parameters": session_state["session_collected_parameters"],
                        "remaining_parameters": session_state["session_remaining_parameters"],
                        "validate_params_state": {"tool_name": None, "valid": {}},
                        "session_state_history": session_history,
                        "switch_intent_status": InfraChatConstants.SWITCH_INTENT_RESUMING,  # Signal that we're resuming
                        "tool_name": session_state["session_tool_name"],
                        "switch_awaiting_selection": False,
                        "switch_matches": [],
                        "turn_user_response": f"Resuming {session_state['session_tool_name']} workflow..."
                    }
            except ValueError:
                pass

            logger.info(f"[SWITCH_INTENT] Invalid selection, showing options again")
            # Invalid selection - show UI again
            return {
                "turn_user_response": "Invalid selection. Please say a number from the list or 'new' to start fresh.",
                "switch_intent_status": "switch_disambiguation"
            }

        # Special case: CREATE → CREATE with different resource = tell user to create new thread
        # Check if there's already a CREATE workflow active or in the stack
        if intent == "CREATE" and resource:
            logger.info(f"[SWITCH_INTENT] Checking CREATE switch: intent={intent}, resource={resource}, running_intent={running_intent}, running_resource={running_resource}")
            # Check active workflow
            if running_intent == "CREATE" and resource != running_resource:
                # Active CREATE is different resource
                logger.info(f"[SWITCH_INTENT] DETECTED CREATE→CREATE switch! Blocking the switch.")
                running_display = _get_resource_display_name(tenant_id, running_resource)
                new_display = _get_resource_display_name(tenant_id, resource)
                msg = f"This ticket is for {running_display}. "
                msg += f"Please create a new ticket for {new_display}."
                return {
                    "switch_intent_status": "switch_blocked",  # Special status to stop execution
                    "turn_intent": "UNSUPPORTED",  # Override to prevent routing to create_flow
                    "turn_user_response": msg
                }

            # Check stack for any CREATE workflows
            session_history = state.get("session_state_history") or {}
            for session_key, session_state in session_history.items():
                session_intent = session_state.get("session_intent")
                session_resource = session_state.get("session_resource")
                session_status = session_state.get("session_status")

                # If there's a CREATE workflow in stack (not rejected/completed) and resource is different
                if session_intent == "CREATE" and session_status in ["pending_confirmation", "active"]:
                    if resource != session_resource:
                        session_display = _get_resource_display_name(tenant_id, session_resource)
                        new_display = _get_resource_display_name(tenant_id, resource)
                        msg = f"This thread is for {session_display}. "
                        msg += f"Please start a new thread for {new_display}."
                        return {
                            "switch_intent_status": "switch_blocked",  # Special status to stop execution
                            "turn_intent": "UNSUPPORTED",  # Override to prevent routing to create_flow
                            "turn_user_response": msg
                        }

        if is_switch:
            # Check if current workflow is incomplete
            current_collected = state.get("collected_parameters") or {}
            current_remaining = state.get("remaining_parameters") or {}
            reference_result = state.get("reference_result")

            # CREATE is incomplete if: has remaining params (waiting for input) or has collected params but not confirmed
            # DISABLED: Allow all switches without confirmation (except CREATE→CREATE different resource which is blocked above)
            # create_incomplete = (
            #     running_intent == "CREATE" and
            #     (current_remaining or current_collected) and
            #     state_hint.get("running_phase") != "confirmation"
            # )
            create_incomplete = False  # Disabled - allow switches without confirmation

            # REFERENCE is incomplete if: has remaining_params or no result yet
            # DISABLED: Don't trigger switch confirmation for REFERENCE workflows
            # reference_incomplete = (
            #     running_intent == "REFERENCE" and
            #     (current_remaining or not reference_result)
            # )
            reference_incomplete = False  # Disabled for now

            logger.info(f"[SWITCH_INTENT] is_switch={is_switch}, create_incomplete={create_incomplete}, reference_incomplete={reference_incomplete}")
            logger.info(f"[SWITCH_INTENT] current_collected={current_collected}, current_remaining={list(current_remaining.keys())}")

            if create_incomplete:  # Removed reference_incomplete check
                # Push current state to stack with status=pending_confirmation
                timestamp = int(time.time() * 1000)
                switch_counter = state.get("switch_counter", 0) + 1

                # Generate unique session key
                session_key = f"{running_intent}_{running_resource}_{timestamp}"

                # Build session state
                session_state = {
                    "session_intent": running_intent,
                    "session_resource": running_resource,
                    "session_phase": state_hint.get("running_phase", "parameter_collection"),
                    "session_collected_parameters": current_collected,
                    "session_remaining_parameters": current_remaining,
                    "session_tool_name": running_tool_name,
                    "session_timestamp": timestamp,
                    "session_status": "pending_confirmation"
                }

                # Get session_state_history
                session_history = state.get("session_state_history") or {}

                # Save to session history
                session_history[session_key] = session_state

                # Build pending_switch info for confirmation
                pending_switch = {
                    "new_intent": intent,
                    "new_resource": resource,
                    "new_tool_name": tool_name,
                    "session_key": session_key,
                    "from_intent": running_intent,
                    "from_resource": running_resource,
                    "from_tool_name": running_tool_name
                }

                # Build confirmation message
                if running_intent == "CREATE":
                    msg = f"You're in the middle of creating {running_resource.upper()}. "
                    msg += f"Switch to {intent.lower()}? (yes/no)"
                else:  # REFERENCE
                    msg = f"You're in the middle of {running_tool_name.replace('_', ' ')}. "
                    msg += f"Switch to {intent.lower()}? (yes/no)"

                logger.info(f"[SWITCH_INTENT] Returning SWITCH_INTENT_DETECTED with confirmation message")
                return {
                    "switch_intent_status": InfraChatConstants.SWITCH_INTENT_DETECTED,
                    "session_state_history": session_history,
                    "pending_switch": pending_switch,
                    "switch_counter": switch_counter,
                    "turn_user_response": msg
                }

    # No switch detected or workflow complete - proceed with normal flow
    # Set state_hint for REFERENCE workflows
    logger.info(f"[SWITCH_INTENT] Reached end of switch logic, intent={intent}, tool_name={tool_name}")

    if intent == "REFERENCE" and tool_name:
        # For REFERENCE: running_resource is the resource being queried (if specified)
        # tool_name is the operation being performed
        state_hint = {
            "running_intent": "REFERENCE",
            "running_resource": resource if resource else "",  # Resource being queried (e.g., "user-api")
            "running_phase": "parameter_collection",
            "tool_name": tool_name  # Tool being used (e.g., "list_config_of_service")
        }
        result = {
            "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH,
            "tool_name": tool_name,
            "state_hint": state_hint
        }
        if clear_pending_switch:
            result["pending_switch"] = None
        if is_switch:
            result["messages"] = []  # Clear messages when switching intents
            result["validate_params_state"] = {"tool_name": None, "valid": {}}
        return result

    # Set state_hint for CREATE workflows to help intent detector in next turn
    if intent == "CREATE" and resource:
        # We're in an active CREATE workflow for this resource
        state_hint = {
            "running_intent": "CREATE",
            "running_resource": resource,
            "running_phase": "parameter_collection"
        }
        result = {
            "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH,
            "state_hint": state_hint
        }
        if clear_pending_switch:
            result["pending_switch"] = None
        if is_switch:
            result["messages"] = []  # Clear messages when switching intents
            result["validate_params_state"] = {"tool_name": None, "valid": {}}
        return result

    # Clear state_hint for other intents or completed workflows
    result = {
        "switch_intent_status": InfraChatConstants.SWITCH_INTENT_PASS_THROUGH,
        "state_hint": {}
    }
    if clear_pending_switch:
        result["pending_switch"] = None
    if is_switch:
        result["messages"] = []  # Clear messages when switching intents
        result["validate_params_state"] = {"tool_name": None, "valid": {}}
    return result
