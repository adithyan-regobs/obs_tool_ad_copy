"""
Unsupported execution node for handling requests outside AWS infrastructure scope.

This node provides a generic response for unsupported requests.
"""
import logging
from typing import Dict, Any

from app.infra_chat_agent.chat_state import ChatState

logger = logging.getLogger(__name__)


def unsupported_execution_node(state: ChatState, config) -> ChatState:
    """
    Process UNSUPPORTED intent with a generic response.

    Args:
        state: Current chat state
        config: Graph configuration

    Returns:
        Updated state with turn_user_response containing a generic message
    """
    user_message: str = state.get("user_message", "").strip()

    logger.info(f"[UNSUPPORTED_EXECUTION] Unsupported request: {user_message[:50]}...")

    return {
        "turn_user_response": """I can help with AWS infrastructure management.

I can assist you with:
• Creating resources: SQS queues, S3 buckets, DynamoDB tables
• Answering questions: About AWS resources and infrastructure

Examples of what you can ask:
- "create sqs" - Create a new SQS queue
- "what is FIFO" - Get help with resources

Please rephrase your request to be related to AWS infrastructure management."""
    }
