"""
Chat History Utility for saving conversation turns to database.

Provides a centralized function to save complete conversation turns including
user messages, workflow trail metadata, and final agent responses.
"""
import logging
from uuid import uuid4
from typing import Dict, Any

from app.infra_chat_agent.chat_state import ChatState
from app.db.session import AsyncSessionLocal
from app.repository.chat_history_repository import ChatHistoryRepository

logger = logging.getLogger(__name__)


async def save_conversation_turn(
    state: ChatState,
    result: Dict[str, Any],
    thread_id: str
) -> None:
    """
    Save complete conversation turn to chat_history including workflow trail.

    Saves 3-N records per conversation turn:
    1. User message (role="user")
    2. All workflow phase records from workflow_trail (role="llm" for LLM executions, role="agent" for deterministic)
    3. Final agent response (role="agent") with is_ready status

    Args:
        state: Current chat state containing user_message, workflow_trail, tenant_id, and user_id
        result: Result dict containing turn_user_response
        thread_id: Thread identifier from config (conversation_id)

    Raises:
        Exception: If database save fails
    """
    user_message = state.get("user_message", "")
    if user_message:
        user_message = user_message.strip()

    agent_response = result.get("turn_user_response", "")
    if agent_response:
        agent_response = agent_response.strip()

    # Debug logging
    print(f"[CHAT_HISTORY_DEBUG] user_message: '{user_message[:50] if user_message else 'EMPTY'}...'")
    print(f"[CHAT_HISTORY_DEBUG] agent_response: '{agent_response[:50] if agent_response else 'EMPTY'}...'")
    print(f"[CHAT_HISTORY_DEBUG] thread_id: {thread_id}")

    # Skip saving if either message is empty
    if not user_message or not agent_response:
        logger.warning("[CHAT_HISTORY] Skipping save - missing user_message or turn_user_response")
        return

    # Extract state values
    intent = state.get("turn_intent")
    resource = state.get("turn_resource")
    placement_parameters = state.get("collected_placement_parameters")
    attribute_parameters = state.get("collected_parameters")
    is_ready = state.get("is_ready", False)
    workflow_trail = state.get("workflow_trail", [])
    state_hint = state.get("state_hint", {})

    # Get tenant and user codes from state
    tenants_mst_code = state.get("tenant_id")  # Get directly from state
    user_mst_code = state.get("user_id")  # Get from state if available

    print(f"[CHAT_HISTORY_DEBUG] tenants_mst_code: '{tenants_mst_code}'")
    print(f"[CHAT_HISTORY_DEBUG] user_mst_code: '{user_mst_code}'")

    try:
        print(f"[CHAT_HISTORY_DEBUG] Starting database save...")
        async with AsyncSessionLocal() as session:
            repo = ChatHistoryRepository(session)
            print(f"[CHAT_HISTORY_DEBUG] Session created, saving user message...")

            # RECORD 1: Save user message
            await repo.create(
                code=str(uuid4()),
                name=f"User message at {intent or 'unknown'}",
                description=f"User message for {resource or 'unknown'} workflow",
                role="user",
                message=user_message,
                intent=intent,
                resource=resource,
                placement_parameters=placement_parameters,
                attribute_parameters=None,  # User message has no attribute params
                is_ready=False,
                llm_model=None,
                llm_purpose=None,
                summary_status=False,
                thread_id=thread_id,
                # Foreign keys and workflow tracking
                tenants_mst_code=tenants_mst_code,
                user_mst_code=user_mst_code,
                workflow_phase=None,
                confidence=None,
                reasoning=None,
                extraction_method=None,
            )

            # RECORDS 2-N: Save workflow phase records from trail
            for phase_data in workflow_trail:
                # Use role="llm" for LLM executions, role="agent" for deterministic
                phase_role = "llm" if phase_data.get("extraction_method") == "llm" else "agent"

                await repo.create(
                    code=str(uuid4()),
                    name=f"{phase_data.get('phase', 'unknown')}: {resource or 'workflow'}",
                    description=f"Workflow phase: {phase_data.get('phase')}",
                    role=phase_role,  # "llm" for LLM executions, "agent" for deterministic
                    message=phase_data.get("message", ""),
                    intent=intent,
                    resource=resource,
                    placement_parameters=placement_parameters,
                    attribute_parameters=attribute_parameters,
                    is_ready=False,  # Intermediate phases not ready
                    llm_model=phase_data.get("llm_model"),
                    llm_purpose=phase_data.get("llm_purpose"),
                    summary_status=False,
                    thread_id=thread_id,
                    # Foreign keys and workflow tracking from trail
                    tenants_mst_code=tenants_mst_code,
                    user_mst_code=user_mst_code,
                    workflow_phase=phase_data.get("phase"),
                    confidence=phase_data.get("confidence"),
                    reasoning=phase_data.get("reasoning"),
                    extraction_method=phase_data.get("extraction_method"),
                )

            # RECORD N+1: Save final agent response
            await repo.create(
                code=str(uuid4()),
                name=f"Agent response at {intent or 'unknown'}",
                description=f"Agent response for {resource or 'unknown'} workflow",
                role="agent",  # Final response is always "agent"
                message=agent_response,
                intent=intent,
                resource=resource,
                placement_parameters=placement_parameters,
                attribute_parameters=attribute_parameters,
                is_ready=is_ready,
                llm_model=None,  # Response handler doesn't use LLM
                llm_purpose="response_generation",
                summary_status=False,
                thread_id=thread_id,
                # Foreign keys and workflow tracking
                tenants_mst_code=tenants_mst_code,
                user_mst_code=user_mst_code,
                workflow_phase=state_hint.get("running_phase", "response_generation"),
                confidence=None,
                reasoning=None,
                extraction_method=None,
            )

            # Commit all records together (atomic)
            print(f"[CHAT_HISTORY_DEBUG] About to commit {2 + len(workflow_trail)} records...")
            await session.commit()
            print(f"[CHAT_HISTORY_DEBUG] Commit successful!")

            logger.info(
                f"[CHAT_HISTORY] Saved complete turn: 1 user + {len(workflow_trail)} phases + 1 response"
                f" = {2 + len(workflow_trail)} records total"
            )

    except Exception as e:
        print(f"[CHAT_HISTORY_DEBUG] ERROR: {str(e)}")
        logger.error(f"[CHAT_HISTORY] Failed to save messages: {str(e)}", exc_info=True)
        raise  # Re-raise to fail the request
