from typing import TypedDict, List, Dict, Any, Optional, Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


# N.B - Turn,slot indicate current turn. The passage through node of current passage.
# Eg: turn_intent - intent detected by the detector of this message.
# slot_parameters - parameters extracted


# Workflow trail entry - tracks metadata from each workflow phase for audit trail
class WorkflowTrailEntry(TypedDict, total=False):
    """Type-safe structure for workflow execution trail entries."""
    phase: str  # Workflow phase: intent_detection, parameter_extraction, validation, qa_execution, etc.
    llm_purpose: str  # Purpose of LLM call: intent_detection, parameter_extraction, qa_generation, etc.
    llm_model: Optional[str]  # LLM model used (e.g., gpt-4o-mini, claude-3-sonnet) or None if deterministic
    confidence: Optional[float]  # LLM confidence score (0.0 to 1.0) - mainly for intent detection
    reasoning: str  # LLM reasoning/explanation or validation result
    extraction_method: str  # Method used: 'llm', 'deterministic', 'manual'
    message: str  # Human-readable message describing what happened
    timestamp: str  # ISO 8601 timestamp of when this phase executed


#Class to help with maintaining state history.
#Necessary parameters to be saved in state switching.
class SessionState(TypedDict, total = False):
    session_intent: str
    session_resource: str
    session_phase: str
    session_collected_parameters: str
    session_remaining_parameters: str
    session_tool_name: str  # For REFERENCE: which tool was being used
    session_timestamp: int  # When this session was created (for sorting)
    session_status: str  # "pending_confirmation" | "active" | "rejected" | "completed"

#Class to provide hint about current state.
#used to provide state info for intent finder at every turn.
#custom logics based on actively running intent , resource and phase.
#Handy in intent/resource switching senarios.
class StateHint(TypedDict, total = False):
    running_intent: str
    running_resource: str  # For CREATE: resource type (sqs, sns, etc.)
    running_phase: str
    tool_name: str  # For REFERENCE: which tool is being used


class ValidateParamsState(TypedDict, total=False):
    """Conversation-scoped state for ValidateParams MCP tool."""
    tool_name: Optional[str]
    valid: Dict[str, Any]


class ChatState(TypedDict, total=False):
    tenant_id: str #should be provided by client
    user_id: str  #should be provided by client.
    user_message: str #set at service provided by client.

    # Information about current turn.
    # We are going to use the keywords
    # turn and slot to refer respective parameters of the turn (current single user pass).
    # These parameters are only relevant to that turn and must be cleared after the turn.
    turn_intent: str
    turn_resource: str
    slot_parameters: Dict[str, Any]
    slot_errors: List[Dict[str,str]]

    #User response provided by our system.
    turn_user_response: str
    turn_requires_user_input: bool  # True when validation failed and user needs to change parameters

    # Guardrails
    guardrail_status: str  # pending | input_blocked
    guardrail_reason: Optional[str]
    validated_user_message: Optional[str]

    

    #Keep session state history. A collection of states kept when switched.
    session_state_history: Dict[str, SessionState]


    #TODO: Check with better arranging we can use SessionState class to represent
    # state hint and info over multiple turns.
    #statehint object
    state_hint: StateHint

    #info about collected parameters and the rest over multiple turns.
    collected_parameters: Dict[str, Any]
    confirmed_parameters: Dict[str, Any]  # Parameters confirmed by user (ephemeral, cleared each turn)
    remaining_parameters: Dict[str, Any] #TODO:this is computable. Double check if you need it.
    is_ready: bool  # True when all required parameters are collected and validated (transient, for API response)
    queue_status: Optional[str]  # Queue status for workflow (e.g., "draft", "approved") - transient, for API response
    has_prompted_for_params: bool  # True after first "I need params" message shown (for extraction feedback)

    # Separate tracking for placement vs attribute parameters
    collected_placement_parameters: Dict[str, Any]  # Placement params collected from user messages
    remaining_placement_parameters: Dict[str, Any]  # Missing required placement params

    # CREATE v2 validator state persisted in LangGraph checkpoint
    validate_params_state: ValidateParamsState


    #Temporary parameters used for mapping.
    parameter_extraction_method: str #Deterministic or llm.
    intent_detection_method_status: str #Deterministic or llm intent detection.
    intent_detection_llm_prechecks_status: str #Prechecks success or failed.

    #Reference workflow specific fields
    tool_name: str #Which reference tool to use (list_services, list_config_of_service, etc.)
    reference_result: Dict[str, Any] #Result from reference tool execution (only set when executed)
    matched_services: List[Dict[str, Any]]  # For REFERENCE list_services - service matches for UI buttons
    remaining_reference_parameters: Dict[str, Any]  # For REFERENCE workflow - parameter options for frontend dropdowns

    #Intent switching and session management
    switch_intent_status: str  # Status of switch intent detection (switch_intent_detected, switch_intent_pass_through, switch_blocked)
    pending_switch: Dict[str, Any]  # {intent, resource, tool_name} waiting for user confirmation
    switch_counter: int  # Incrementing counter for session_state IDs

    # Workflow execution trail - NEW for chat history context
    # Type-safe list tracking metadata from each workflow phase (intent detection, parameter extraction, validation, etc.)
    # Used to save complete audit trail to database at end of turn
    workflow_trail: List[WorkflowTrailEntry]
    # Resource cases/use-cases (e.g., ["create_bucket"], ["create_queue"])
    cases: Optional[List[str]]

    # Messages for REFERENCE workflow tool loops
    # Uses add_messages reducer for proper LangGraph tool loop handling
    # Note: Only used within-request; cross-request uses DB history as text context
    messages: Annotated[List[BaseMessage], add_messages]
