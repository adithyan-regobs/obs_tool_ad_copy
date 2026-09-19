"""Simple state for infra_chat_agent_with_tools."""

from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """Minimal state for the agent graph."""

    # Conversation messages (accumulates via add_messages reducer)
    messages: Annotated[list, add_messages]

    # Current detected intent: "REFERENCE" | "QA" | "CREATE" | "UNSUPPORTED"
    current_intent: str
