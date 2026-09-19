import asyncio
import logging
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.infra_chat_agent.infra_chat_graph import infra_chat_graph
from app.schemas.infra_chat_schemas import InfraChatRequestSchema, UpdatePlacementParamsRequestSchema, ClearStateRequestSchema
from app.services.langfuse_service import langfuse_service
from app.repository.chat_history_repository import ChatHistoryRepository
from app.db.models.chat_history_model import ChatHistoryModel

logger = logging.getLogger(__name__)

# Singleton graph instance - shared across all ChatService instances
_graph_instance = None
_graph_lock = asyncio.Lock()


async def _get_graph_async():
    """
    Get or create the singleton graph instance (async).

    Graph initialization is now async because MCP tools require async context.
    Uses a lock to ensure single initialization even with concurrent requests.
    """
    global _graph_instance
    if _graph_instance is None:
        async with _graph_lock:
            # Double-check after acquiring lock
            if _graph_instance is None:
                logger.info("Initializing infra_chat_graph (async singleton)...")
                _graph_instance = await infra_chat_graph()
                logger.info("infra_chat_graph initialized successfully")
    return _graph_instance


class ChatService:
    """
    Service for processing chat messages through the infra chat agent graph.
    """

    def __init__(self):
        """Initialize the chat service. Graph is initialized lazily on first use."""
        self._graph = None

    async def _get_graph(self):
        """Get the graph instance, initializing if needed."""
        if self._graph is None:
            self._graph = await _get_graph_async()
        return self._graph

    async def process_message(
        self,
        request: InfraChatRequestSchema,
    ) -> dict:
        # Get graph instance (lazy async init)
        graph = await self._get_graph()

        # Use tenant_id and user_id from request (populated by JWT)
        tenant_id = request.tenants_mst_code or "default"
        user_id = request.user_mst_code or "anonymous"

        # Make thread_id tenant-scoped for isolation
        thread_id = request.conversation_id
        logger.info(f"[THREAD: {thread_id}] Processing message via ChatService")

        # Prepare initial state for the graph
        initial_state = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "user_message": request.message,
        }

        # Get Langfuse callback handler for automatic tracing
        langfuse_handler = langfuse_service.get_callback_handler(
            trace_name="infra_chat_workflow"
        )

        # Build config with thread_id and callbacks
        config = {"configurable": {"thread_id": thread_id}}
        if langfuse_handler:
            config["callbacks"] = [langfuse_handler]
            # Add metadata via config dict (Langfuse v3 pattern)
            config["metadata"] = {
                "langfuse_user_id": user_id,
                "langfuse_session_id": thread_id,
                "langfuse_tags": ["infra_chat"],
                "tenant_id": tenant_id,
                "conversation_id": request.conversation_id,
                "message_preview": request.message[:100]
            }

        # Invoke the graph with thread_id for checkpointing (using async ainvoke)
        result = await graph.ainvoke(initial_state, config)

        return result

    async def update_placement_parameters(
        self,
        request: UpdatePlacementParamsRequestSchema,
    ) -> Dict[str, Any]:
        """
        Update placement parameters for a conversation.

        This method can be called:
        1. Before any conversation exists (to pre-populate placement params)
        2. During an active conversation (to update existing placement params)

        """
        # Get graph instance (lazy async init)
        graph = await self._get_graph()

        # Use tenant_id and user_id from request (populated by JWT)
        tenant_id = request.tenants_mst_code or "default"
        user_id = request.user_mst_code or "anonymous"

        # Make thread_id tenant-scoped for isolation
        thread_id = request.conversation_id

        try:
            config = {"configurable": {"thread_id": thread_id}}

            # Try to get current state
            current_state = await graph.aget_state(config)

            # Case 1: No existing checkpoint - initialize with placement parameters
            if not current_state or not current_state.values:

                # Create initial state with minimum required fields
                initial_state = {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "collected_placement_parameters": request.placement_parameters,
                }

                # This will create a new checkpoint with these values
                await graph.aupdate_state(config, initial_state, as_node="__start__")

                return {
                    "status": "success",
                    "collected_placement_parameters": request.placement_parameters,
                    "message": "Placement parameters initialized successfully"
                }

            # Case 2: Checkpoint exists - merge with existing placement parameters
            existing_placement_params = current_state.values.get("collected_placement_parameters", {})
            updated_placement_params = {**existing_placement_params, **request.placement_parameters}

            # Update only the placement parameters field
            await graph.aupdate_state(
                config,
                {"collected_placement_parameters": updated_placement_params},
                as_node="__start__"
            )

            return {
                "status": "success",
                "collected_placement_parameters": updated_placement_params,
                "message": "Placement parameters updated successfully"
            }

        except Exception as e:
            logger.error(
                f"[BOT_SERVICE] Failed to update placement params for thread {thread_id}: {str(e)}",
                exc_info=True
            )
            return {
                "status": "error",
                "message": f"Failed to update placement parameters: {str(e)}",
                "collected_placement_parameters": {}
            }

    async def clear_state(
        self,
        request: ClearStateRequestSchema,
    ) -> Dict[str, Any]:
        """
        Clear conversation state (simulates resource creation completion).

        Clears the same state values that are cleared when a resource is created:
        - collected_parameters (attribute parameters)
        - remaining_parameters
        - state_hint (workflow state)

        Preserves:
        - collected_placement_parameters (for reuse)
        - session_state_history
        - Other persisted state
        """
        # Get graph instance (lazy async init)
        graph = await self._get_graph()

        # Use tenant_id and user_id from request (populated by JWT)
        tenant_id = request.tenants_mst_code or "default"
        user_id = request.user_mst_code or "anonymous"

        # Make thread_id tenant-scoped for isolation
        thread_id = request.conversation_id

        try:
            config = {"configurable": {"thread_id": thread_id}}

            # Get current state
            current_state = await graph.aget_state(config)

            # Check if checkpoint exists
            if not current_state or not current_state.values:
                logger.info(
                    f"[BOT_SERVICE] No existing checkpoint for thread {thread_id}, nothing to clear"
                )
                return {
                    "status": "success",
                    "message": "No state to clear (conversation not started)",
                    "cleared_items": []
                }

            # Clear the state values that get cleared on resource creation
            cleared_items: List[str] = []

            updates = {}
            if current_state.values.get("collected_parameters"):
                updates["collected_parameters"] = {}
                cleared_items.append("collected_parameters")

            if current_state.values.get("remaining_parameters"):
                updates["remaining_parameters"] = {}
                cleared_items.append("remaining_parameters")

            if current_state.values.get("state_hint"):
                updates["state_hint"] = {}
                cleared_items.append("state_hint")

            if current_state.values.get("validate_params_state"):
                updates["validate_params_state"] = {
                    "tool_name": None,
                    "valid": {},
                }
                cleared_items.append("validate_params_state")

            # Apply updates
            if updates:
                await graph.aupdate_state(config, updates, as_node="__start__")
                logger.info(
                    f"[BOT_SERVICE] Cleared state for thread {thread_id}: {cleared_items}"
                )

            return {
                "status": "success",
                "message": f"Successfully cleared {len(cleared_items)} state item(s)",
                "cleared_items": cleared_items
            }

        except Exception as e:
            logger.error(
                f"[BOT_SERVICE] Failed to clear state for thread {thread_id}: {str(e)}",
                exc_info=True
            )
            return {
                "status": "error",
                "message": f"Failed to clear state: {str(e)}",
                "cleared_items": []
            }

    def get_response_text(self, result: dict) -> str:
        """
        Extract a human-readable response from the graph result.

        Args:
            result: Result dict from process_message

        Returns:
            Human-readable response text
        """
        # Try to get the generated response
        if result.get("turn_user_response"):
            return result["turn_user_response"]

        # Fallback: Build response from state
        intent = result.get("turn_intent", "UNKNOWN")
        resource = result.get("turn_resource", "")
        rbac_status = result.get("rbac_status", "")
        rbac_reason = result.get("rbac_reason", "")

        if rbac_status == "denied":
            return f"Access Denied: {rbac_reason}"

        if intent == "CREATE":
            return f"Creating {resource}..." if resource else "Creating resource..."
        elif intent == "REFERENCE":
            tool_name = result.get("slot_parameters", {}).get("tool_name", "")
            return f"Looking up {tool_name}..." if tool_name else "Looking up information..."
        elif intent == "QA":
            return "I'm here to help answer your questions."
        elif intent == "UNSUPPORTED":
            return "I'm sorry, I can't help with that request. I'm designed to help with AWS infrastructure management."
        else:
            return f"Intent detected: {intent}"

    async def get_conversation_history(
        self,
        db: AsyncSession,
        tenant_code: str,
        conversation_id: str,
        ticket_id: Optional[int] = None,
        intent: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[ChatHistoryModel]:
        """
        Get conversation display history for UI (excludes internal LLM workflow trail).

        This method retrieves only user messages and final agent responses,
        filtering out internal LLM execution records (intent detection, parameter extraction, etc.)
        that are stored for audit and context purposes.

        Args:
            db: Database session
            tenant_code: Tenant code
            conversation_id: Conversation identifier
            intent: Optional filter by intent (CREATE, REFERENCE, QA, UNSUPPORTED)
            limit: Optional limit on number of messages

        Returns:
            List of ChatHistoryModel objects with role in ["user", "agent"]
        """
        # Initialize repository
        repo = ChatHistoryRepository(db)

        if ticket_id is not None:
            messages = await repo.get_user_agent_conversation_display_history_by_ticket_id(
                ticket_id=ticket_id,
                tenant_code=tenant_code,
                intent=intent,
                limit=limit
            )
        else:
            # Get conversation display history (excludes role="llm")
            messages = await repo.get_user_agent_conversation_display_history(
                thread_id=conversation_id,
                intent=intent,
                limit=limit
            )

        logger.info(
            f"[BOT_SERVICE] Retrieved {len(messages)} conversation messages "
            f"for conversation {conversation_id}"
        )

        return messages
