from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.infra_chat_constants import InfraChatConstants


def parameter_extraction_deterministic_node(state: ChatState) -> ChatState:
    """
    Deterministic parameter extraction node for CREATE workflow.

    Tries to extract parameters using deterministic/rules-based approach.
    Falls back to LLM if deterministic extraction fails.

    Currently passes through to LLM.
    """
    # TODO: Implement deterministic parameter extraction
    # - Parse user message for key-value pairs
    # - Check for known parameter formats
    # - Validate against tenant config

    return {
        "parameter_extraction_method": InfraChatConstants.LLM_PARAM_EXTRACTION_REQUIRED
    }
