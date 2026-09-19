from typing import Optional
from langchain_openai import ChatOpenAI

from app.infra_chat_agent.llm_config_util import LLMConfigUtil
from app.core.config import settings


def get_llm_client(
    tenant_id: str,
    intent: str,
    temperature: float = 0.0,
    model_override: Optional[str] = None
) -> ChatOpenAI:
    """
    Get ChatOpenAI client with model based on tenant and intent.

    Uses LLMConfigUtil to determine which model to use for the given
    tenant and intent combination.

    Args:
        tenant_id: Tenant identifier
        intent: Intent type (e.g., "CREATE", "REFERENCE", "QA")
        temperature: LLM temperature (default: 0.0)
        model_override: If provided, use this model instead of config lookup

    Returns:
        ChatOpenAI instance configured with the appropriate model
    """
    # Use override if provided, otherwise get from config
    model = model_override or LLMConfigUtil.get_llm_model(tenant_id, intent)

    # Create and return ChatOpenAI client
    return ChatOpenAI(
        api_key=settings.openai_api_key,
        model=model,
        temperature=temperature,
    )
