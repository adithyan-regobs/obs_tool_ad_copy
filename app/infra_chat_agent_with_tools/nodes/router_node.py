"""
Router node for intent classification.

Classifies user message into: REFERENCE, QA, UNSUPPORTED
Tool selection is delegated to reference_node.
"""
from typing import Literal
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.infra_chat_agent_with_tools.state import AgentState
from app.core.config import settings


class RouteDecision(BaseModel):
    """Structured output for intent detection."""
    intent_category: Literal["REFERENCE", "QA", "UNSUPPORTED"] = Field(
        description="The detected intent category"
    )
    intent_target: Literal["UNKNOWN"] = Field(
        default="UNKNOWN",
        description="Target resource (reserved for future CREATE)"
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="Confidence level of the classification"
    )


ROUTER_SYSTEM_PROMPT = """You are an intent classifier for a service configuration assistant.

Classify the user's message into one of these categories:

## Intent Categories:
- **REFERENCE**: User wants to query existing service configurations
  - "list services"
  - "show config for user-api"
  - "what services are configured?"
  - "show me the configuration of order-service"

- **QA**: General questions, greetings, or explanations
  - "what is a service config?"
  - "hello"
  - "how does this work?"
  - "explain CPU allocation"

- **UNSUPPORTED**: Request doesn't fit any category or is unclear

## Confidence Levels:
- **high**: Clear, explicit intent
- **medium**: Implied intent but context helps
- **low**: Ambiguous or unclear

## Rules:
1. Consider conversation history to interpret follow-up messages
2. Short replies like "yes", "show me" should use context from previous messages
3. Always set intent_target to "UNKNOWN" (reserved for future use)
"""


_router_llm = None


def get_router_llm():
    """Lazy initialization of router LLM."""
    global _router_llm
    if _router_llm is None:
        _router_llm = ChatOpenAI(
            api_key=settings.openai_api_key,
            model="gpt-4o",
            temperature=0
        )
    return _router_llm


def router_node(state: AgentState) -> dict:
    """
    Classify user intent from the latest message.

    Args:
        state: Current agent state with messages

    Returns:
        dict with current_intent set
    """
    messages = state.get("messages", [])
    if not messages:
        return {"current_intent": "UNSUPPORTED"}

    # Build context from conversation history
    context_parts = []
    for msg in messages[:-1]:  # Exclude latest message
        role = "User" if msg.type == "human" else "Assistant"
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        context_parts.append(f"{role}: {content}")

    context = "\n".join(context_parts) if context_parts else "No prior context"

    # Get latest user message
    latest_message = messages[-1]
    user_content = latest_message.content if isinstance(latest_message.content, str) else str(latest_message.content)

    # Build classification prompt
    classification_prompt = f"""Recent conversation:
{context}

Current user message: {user_content}

Classify this message."""

    # Use structured output
    structured_llm = get_router_llm().with_structured_output(RouteDecision)

    result = structured_llm.invoke([
        SystemMessage(content=ROUTER_SYSTEM_PROMPT),
        HumanMessage(content=classification_prompt)
    ])

    return {"current_intent": result.intent_category}


def route_by_intent(state: AgentState) -> str:
    """
    Route to appropriate node based on intent.

    Args:
        state: Current agent state

    Returns:
        Node name to route to
    """
    intent = state.get("current_intent", "UNSUPPORTED")

    if intent == "REFERENCE":
        return "reference_node"
    elif intent == "QA":
        return "qa_node"
    else:
        return "end"
