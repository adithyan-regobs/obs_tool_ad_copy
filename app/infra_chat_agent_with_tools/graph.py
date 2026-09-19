"""
LangGraph builder for infra_chat_agent_with_tools.

Lazy singleton pattern - initializes on first request.
No main.py hooks needed.
"""
import asyncio
import logging
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.infra_chat_agent_with_tools.state import AgentState
from app.infra_chat_agent_with_tools.nodes.router_node import router_node, route_by_intent
from app.infra_chat_agent_with_tools.nodes.reference_node import create_reference_node
from app.infra_chat_agent_with_tools.nodes.qa_node import qa_node

logger = logging.getLogger(__name__)


# MCP server configuration
MCP_SERVERS = {
    "infra-chat-tools": {
        "command": "python",
        "args": ["-m", "app.infra_chat_agent_with_tools.mcp_server.server"],
        "transport": "stdio"
    }
}


# Lazy singleton state
_mcp_client: MultiServerMCPClient | None = None
_tools: list | None = None
_graph = None
_checkpointer: MemorySaver | None = None
_initialized = False
_lock = asyncio.Lock()


async def get_graph():
    """
    Get or create singleton graph (lazy initialization).

    Thread-safe with async lock. First request pays init cost,
    subsequent requests reuse the singleton.

    Returns:
        Compiled LangGraph graph with checkpointer
    """
    global _mcp_client, _tools, _graph, _checkpointer, _initialized

    if _initialized:
        return _graph

    async with _lock:
        # Double-check after acquiring lock
        if _initialized:
            return _graph

        logger.info("=" * 60)
        logger.info("INFRA CHAT WITH TOOLS - Lazy initializing (first request)")
        logger.info("=" * 60)

        # Create MCP client (spawns subprocess)
        _mcp_client = MultiServerMCPClient(MCP_SERVERS)

        # Load tools
        _tools = await _mcp_client.get_tools()
        logger.info(f"MCP tools loaded: {[t.name for t in _tools]}")

        # Create checkpointer for conversation persistence
        _checkpointer = MemorySaver()

        # Build graph with tools
        _graph = _build_graph(_tools, _checkpointer)
        _initialized = True

        logger.info("Graph initialized successfully")
        logger.info("=" * 60)

    return _graph


def should_continue(state: AgentState) -> str:
    """Check if last message has tool calls."""
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


def _build_graph(tools: list, checkpointer: MemorySaver):
    """
    Build the state graph with nodes and edges.

    Args:
        tools: Tools from MCP
        checkpointer: Checkpointer for state persistence

    Returns:
        Compiled graph with checkpointer
    """
    # Create nodes
    reference_node = create_reference_node(tools)
    tool_node = ToolNode(tools)

    # Build graph
    builder = StateGraph(AgentState)

    # Add nodes
    builder.add_node("router_node", router_node)
    builder.add_node("reference_node", reference_node)
    builder.add_node("qa_node", qa_node)
    builder.add_node("tools", tool_node)

    # Set entry point
    builder.set_entry_point("router_node")

    # Router → intent-based routing
    builder.add_conditional_edges(
        "router_node",
        route_by_intent,
        {
            "reference_node": "reference_node",
            "qa_node": "qa_node",
            "end": END,
        }
    )

    # Reference node → check for tool calls
    builder.add_conditional_edges(
        "reference_node",
        should_continue,
        {
            "tools": "tools",
            END: END,
        }
    )

    # Tools → back to reference node
    builder.add_edge("tools", "reference_node")

    # QA node → end
    builder.add_edge("qa_node", END)

    return builder.compile(checkpointer=checkpointer)
