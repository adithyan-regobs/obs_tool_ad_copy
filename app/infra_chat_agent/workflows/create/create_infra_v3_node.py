"""
Create Infrastructure V3 node.

Generic node for infrastructure resource creation operations (S3, SQS, DynamoDB, etc.).
Designed to be the default entry point for resource creation flows that use MCP tools.
"""
import logging
from typing import List

from app.infra_chat_agent.chat_state import ChatState

logger = logging.getLogger(__name__)


def create_infra_v3_node(tools: list):
    """
    Create infrastructure V3 node function.

    Args:
        tools: List of MCP tools to bind for this resource type.

    Returns:
        Async node function
    """

    async def create_infra_v3(state: ChatState, config) -> dict:
        """
        Handle infrastructure resource creation.

        Args:
            state: Current chat state
            config: Graph configuration (contains thread_id)

        Returns:
            Dict with updated state
        """
        pass

    return create_infra_v3
