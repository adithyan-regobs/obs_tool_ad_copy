from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.infra_chat_constants import InfraChatConstants

def intent_detector_llm_pre_checks_node(state: ChatState) -> ChatState:

    #Currently just passing through. Send it to llm
    return{

        "intent_detection_llm_prechecks_status": InfraChatConstants.INTENT_DETECTOR_LLM_PRE_CHECKS_SUCCESS
    }