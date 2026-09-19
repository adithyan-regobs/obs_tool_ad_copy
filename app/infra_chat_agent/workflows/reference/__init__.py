"""
REFERENCE workflow.

Handles information requests about services, configurations, and parameters.
Uses MCP tools pattern with LLM.bind_tools() for tool selection.
"""
from app.infra_chat_agent.workflows.reference.reference_node import create_reference_node

__all__ = ["create_reference_node"]
