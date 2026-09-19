"""
Legacy handler node for Service Config Agent.

Thin orchestrator: dispatches to intent handlers based on detected intent.
Preserves existing behavior during Phase 1 transition.
"""

from typing import Dict, Any

from app.services.service_config_agent.state import ServiceConfigAgentState


async def legacy_handler_node(
    state: ServiceConfigAgentState,
    intent_handler,  # Injected: handles all legacy intents
) -> Dict[str, Any]:
    """
    Legacy handler node: dispatches to appropriate intent handler.

    Thin orchestrator that delegates actual logic to injected handler.

    Args:
        state: Current agent state with intent already set
        intent_handler: Service containing all _handle_* methods

    Returns:
        Updated state with response data populated.
    """
    intent = state["intent"]
    message = state["user_message"]
    context = state["context"]
    tenants_mst_code = state["tenants_mst_code"]
    reference_service_code = state.get("reference_service_code")
    history = state.get("history", [])

    # Delegate to intent handler
    response_data = await intent_handler.handle(
        intent=intent,
        message=message,
        context=context,
        tenants_mst_code=tenants_mst_code,
        reference_service_code=reference_service_code,
        history=history,
    )

    # Return updated state fields
    return {
        "response": response_data.get("response", ""),
        "matched_services": response_data.get("matched_services"),
        "reference_service_code": response_data.get("reference_service_code"),
        "reference_service_name": response_data.get("reference_service_name"),
        "config_json": response_data.get("config_json"),
        "updation_field": response_data.get("updation_field"),
        "is_ready": response_data.get("is_ready", False),
    }
