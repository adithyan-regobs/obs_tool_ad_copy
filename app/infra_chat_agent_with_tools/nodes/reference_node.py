"""
Reference node with LLM and bound tools.

LLM decides which tool to call (list_services or show_service_config).
"""
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

from app.infra_chat_agent_with_tools.state import AgentState
from app.core.config import settings


REFERENCE_SYSTEM_PROMPT = """You are a service configuration assistant.

You have access to tools to help users query service configurations:

1. **list_services**: List all services that have configurations
   - Use when user asks to "list services", "show services", "what services are configured?"
   - Requires: environment, geo_loc_code

2. **show_service_config**: Show configuration for a specific service
   - Use when user asks about a specific service's config
   - Requires: service_name, environment, geo_loc_code

Always use the appropriate tool to answer the user's question.
If you don't have required parameters, ask the user for them.
"""


_reference_llm_with_tools = None


def create_reference_node(tools: list):
    """
    Create a reference node with tools bound to LLM.

    Args:
        tools: List of tools to bind (from MCP adapter)

    Returns:
        Node function that invokes LLM with tools
    """
    def get_llm_with_tools():
        """Lazy initialization of LLM with tools."""
        global _reference_llm_with_tools
        if _reference_llm_with_tools is None:
            llm = ChatOpenAI(
                api_key=settings.openai_api_key,
                model="gpt-4o",
                temperature=0
            )
            _reference_llm_with_tools = llm.bind_tools(tools)
        return _reference_llm_with_tools

    def reference_node(state: AgentState) -> dict:
        """
        Invoke LLM with tools to handle REFERENCE intent.

        Args:
            state: Current agent state with messages

        Returns:
            dict with updated messages
        """
        messages = state.get("messages", [])

        # Add system prompt if not present
        if not messages or messages[0].type != "system":
            messages = [SystemMessage(content=REFERENCE_SYSTEM_PROMPT)] + list(messages)

        # Invoke LLM with tools
        response = get_llm_with_tools().invoke(messages)

        return {"messages": [response]}

    return reference_node
