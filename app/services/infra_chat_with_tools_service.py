"""
Service layer for infra_chat_agent_with_tools.

Orchestrates graph invocation with checkpointed state.
"""
import logging
from langchain_core.messages import HumanMessage

from app.infra_chat_agent_with_tools.graph import get_graph

logger = logging.getLogger(__name__)


class InfraChatWithToolsService:
    """
    Service for infra chat with tools.

    Invokes the LangGraph agent with checkpointed state.
    """

    async def chat(self, message: str, thread_id: str, context: dict) -> dict:
        """
        Process a chat message through the agent graph.

        Args:
            message: User message
            thread_id: Conversation ID for state persistence
            context: Request context (environment, geo_loc_code, etc.)

        Returns:
            Raw graph output (format later)
        """
        logger.info(
            "InfraChatWithToolsService.chat",
            extra={
                "message": message,
                "thread_id": thread_id,
                "context": context
            }
        )

        # Get singleton graph (lazy init on first call)
        graph = await get_graph()

        # Config with thread_id for checkpointer
        config = {"configurable": {"thread_id": thread_id}}

        # Invoke graph with checkpointed state
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=message)]},
            config
        )

        return result
