"""
Synthesizer Node for Service Config Agent.

Thin orchestrator that delegates to ResponseSynthesizer service.
Single responsibility: wire state to synthesizer and update state with response.
"""

from typing import Dict, Any, Protocol, runtime_checkable, List, Optional

from app.services.service_config_agent.state import ServiceConfigAgentState
from app.schemas.service_config_chat_schemas import ServiceMatchSchema
from app.services.service_config_agent.tools.autofill_extractor import extract_autofill_candidate
from app.services.service_config_agent.tools.autofill_builder import append_autofill_offer


@runtime_checkable
class ResponseSynthesizerProtocol(Protocol):
    """Contract for response synthesis."""

    async def synthesize(
        self,
        query: str,
        tool_results: List[Dict[str, Any]],
    ) -> str:
        """Synthesize tool results into response."""
        ...


async def synthesizer_node(
    state: ServiceConfigAgentState,
    synthesizer: ResponseSynthesizerProtocol,
) -> Dict[str, Any]:
    """
    Synthesizer node - generates response from tool results.

    Thin orchestrator that:
    1. Extracts tool results from state
    2. Delegates to ResponseSynthesizer for text generation
    3. Delegates to extract_autofill_candidate for autofill detection
    4. Delegates to append_autofill_offer for final response
    5. Updates state with response and updation_field

    Args:
        state: Current workflow state
        synthesizer: Injected ResponseSynthesizer service

    Returns:
        State updates with response and optional updation_field
    """
    tool_results = state.get("tool_results", [])
    user_message = state.get("user_message", "")

    # Handle case with no tool results (e.g., planning failed or ambiguous query)
    if not tool_results:
        error = state.get("error", "")
        if "No tool calls" in error or not error:
            # Query was too ambiguous for the planner
            return {
                "response": (
                    "I need a bit more detail to help you. Could you please specify:\n"
                    "- Which **service** are you asking about?\n"
                    "- Which **parameter** (e.g., CPU, memory, port)?\n\n"
                    "For example: \"What is the CPU for payment-service in staging?\""
                ),
                "intent": "clarification_needed",
            }
        else:
            return {
                "response": f"I'm sorry, I couldn't process your request: {error}",
                "intent": "error",
            }

    # Check for service ambiguity (provides clickable buttons)
    ambiguity_response = _check_service_ambiguity(tool_results)
    if ambiguity_response:
        return ambiguity_response

    # Delegate to synthesizer for response text
    response = await synthesizer.synthesize(
        query=user_message,
        tool_results=tool_results,
    )

    # Extract autofill candidate (pure function)
    context = state.get("context")
    infrastructure_type = context.infrastructure_type if context else None

    autofill = extract_autofill_candidate(
        tool_results=tool_results,
        infrastructure_type=infrastructure_type,
    )

    # Build final response with autofill offer (pure function)
    final_response = append_autofill_offer(response, autofill)

    # Build state update
    # Note: Don't include updation_field here - it triggers auto-fill before user confirms.
    # The offer text ("Would you like me to fill...") is in final_response.
    # When user says "yes", legacy form_fill handler extracts field/value from
    # history and returns updation_field at that point.
    return {
        "response": final_response,
        "intent": "agentic_query",
    }


def _check_service_ambiguity(
    tool_results: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """
    Check if tool results contain service ambiguity.

    Returns structured response with matched_services for clickable buttons.
    """
    for result in tool_results:
        tool_result = result.get("result", {})

        # Check if this is a service ambiguity error with matched_services
        if not tool_result.get("success", False):
            matched_services_data = tool_result.get("matched_services")

            if matched_services_data:
                # Convert to ServiceMatchSchema format for frontend
                matched_services = [
                    ServiceMatchSchema(
                        service_code=s.get("service_code", ""),
                        service_name=s.get("service_name", ""),
                        service_type=s.get("service_type", "API"),
                        similarity_score=s.get("similarity_score", 0.9),
                        has_existing_config=s.get("has_existing_config", True),
                    )
                    for s in matched_services_data
                ]

                return {
                    "response": (
                        "I found multiple services matching your query. "
                        "Please select which one you meant:"
                    ),
                    "intent": "service_ambiguity",
                    "matched_services": matched_services,
                }

    return None
