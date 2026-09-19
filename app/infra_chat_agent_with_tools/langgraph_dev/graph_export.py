"""
Graph Export for LangGraph Dev.

Exports a compiled graph for use with `langgraph dev`.
"""
import asyncio
import logging
import sys
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.infra_chat_agent_with_tools.state import AgentState
from app.infra_chat_agent_with_tools.nodes.router_node import router_node, route_by_intent
from app.infra_chat_agent_with_tools.nodes.reference_node import create_reference_node
from app.infra_chat_agent_with_tools.nodes.qa_node import qa_node

logger = logging.getLogger(__name__)


# MCP server configuration
MCP_SERVERS = {
    "infra-chat-tools": {
        "command": sys.executable,
        "args": ["-m", "app.infra_chat_agent_with_tools.mcp_server.server"],
        "transport": "stdio"
    }
}


def should_continue(state: AgentState) -> str:
    """Check if last message has tool calls."""
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


async def _build_graph():
    """Build the graph with MCP tools asynchronously."""
    logger.info("Initializing MCP client for langgraph dev...")

    mcp_client = MultiServerMCPClient(MCP_SERVERS)
    tools = await mcp_client.get_tools()

    logger.info(f"MCP tools loaded: {[t.name for t in tools]}")

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

    # Compile WITHOUT checkpointer - langgraph dev provides its own
    return builder.compile()


# Build the graph at module load time
graph = asyncio.run(_build_graph())
