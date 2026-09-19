
class InfraChatConstants:

    #Constant literals used across graph.
    DETERMINISTIC_INTENT_DETECTION_SUCCESS: str = "deterministic_intent_detection_success"
    LLM_INTENT_DETECTION_REQUIRED: str = "llm_intent_detection_required"

    INTENT_DETECTOR_LLM_PRE_CHECKS_SUCCESS: str = "llm_inent_detection_pre_checks_success"
    INTENT_DETECTOR_LLM_PRE_CHECKS_FAILED: str = "llm_intent_detection_pre_checks_failed"

    # Switch intent constants
    SWITCH_INTENT_PASS_THROUGH: str = "switch_intent_pass_through"
    SWITCH_INTENT_DETECTED: str = "switch_intent_detected"
    SWITCH_INTENT_REQUIRED: str = "switch_intent_required"
    SWITCH_INTENT_RESUMING: str = "switch_intent_resuming"  # Workflow resumed from session_history

    # Parameter extraction constants
    DETERMINISTIC_PARAM_EXTRACTION_SUCCESS: str = "deterministic_param_extraction_success"
    LLM_PARAM_EXTRACTION_REQUIRED: str = "llm_param_extraction_required"

    PARAM_EXTRACTION_LLM_PRE_CHECKS_SUCCESS: str = "param_extraction_llm_pre_checks_success"
    PARAM_EXTRACTION_LLM_PRE_CHECKS_FAILED: str = "param_extraction_llm_pre_checks_failed"

