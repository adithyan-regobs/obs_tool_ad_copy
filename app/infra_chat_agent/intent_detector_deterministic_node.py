from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.infra_chat_constants import InfraChatConstants

def intent_detector_deterministic_node(state: ChatState) -> ChatState:

    #Currently just passing through. Send it to llm
    return{

        "intent_detection_method_status": InfraChatConstants.LLM_INTENT_DETECTION_REQUIRED
    }