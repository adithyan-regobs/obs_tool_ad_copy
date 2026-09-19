"""
QA node for general questions.

Pure LLM - no tools bound.
"""
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

from app.infra_chat_agent_with_tools.state import AgentState
from app.core.config import settings


QA_SYSTEM_PROMPT = """You are a helpful assistant for infrastructure and service configuration questions.

Answer general questions about:
- What service configurations are
- How infrastructure works
- Explanations of concepts like CPU, memory, scaling
- General greetings and help

Be concise and helpful. If the user wants to query specific service data,
suggest they ask to "list services" or "show config for <service-name>".
"""


_qa_llm = None


def get_qa_llm():
    """Lazy initialization of QA LLM."""
    global _qa_llm
    if _qa_llm is None:
        _qa_llm = ChatOpenAI(
            api_key=settings.openai_api_key,
            model="gpt-4o",
            temperature=0
        )
    return _qa_llm


def qa_node(state: AgentState) -> dict:
    """
    Handle QA intent with pure LLM (no tools).

    Args:
        state: Current agent state with messages

    Returns:
        dict with updated messages
    """
    messages = state.get("messages", [])

    # Add system prompt if not present
    if not messages or messages[0].type != "system":
        messages = [SystemMessage(content=QA_SYSTEM_PROMPT)] + list(messages)

    # Invoke LLM without tools
    response = get_qa_llm().invoke(messages)

    return {"messages": [response]}
