"""
Input guardrail node.

Runs safety checks before intent detection. On block, returns a user-facing
refusal message and short-circuits the flow via response_handler_node.
"""
import logging
from typing import Optional, Dict, Any

from app.infra_chat_agent.chat_state import ChatState
from app.core.config import settings
from app.infra_chat_agent.guardrails.guardrail_config import (
    ContentModerationResult,
    PromptInjectionResult,
    PIIResult,
    GuardrailConfigUtil,
)
from app.infra_chat_agent.guardrails.guardrail_helper import (
    build_guardrail_metadata,
    build_invoke_config,
    run_guardrail_check,
    create_pii_detector,
    create_injection_detector,
)

logger = logging.getLogger(__name__)

async def _check_content_moderation(
    user_message: str,
    metadata: Dict[str, Any],
    model_name: str,
) -> ContentModerationResult:
    system_prompt = (
        "You are a content moderation system for an infrastructure assistant. "
        "Evaluate whether the input contains unsafe or policy-violating content. "
        "Be strict but avoid false positives."
    )
    return await run_guardrail_check(
        ContentModerationResult,
        system_prompt,
        user_message,
        "guardrail_content",
        metadata,
        model_name,
    )


async def _check_prompt_injection(
    user_message: str,
    metadata: Dict[str, Any],
    model_name: str,
) -> PromptInjectionResult:
    detector = create_injection_detector(model_name)
    invoke_config = build_invoke_config("guardrail_injection", metadata)
    return await detector.ainvoke({"text": user_message}, config=invoke_config)


async def _check_pii(
    user_message: str,
    metadata: Dict[str, Any],
    model_name: str,
) -> PIIResult:
    detector = create_pii_detector(model_name)
    invoke_config = build_invoke_config("guardrail_pii", metadata)
    return await detector.ainvoke({"text": user_message}, config=invoke_config)


async def input_guardrail_node(state: ChatState, config) -> ChatState:
    """LLM-based input guardrails for infra-chat."""
    if not settings.infra_chat_guardrails_enabled:
        return {}

    user_message = (state.get("user_message") or "").strip()
    if not user_message:
        return {}

    metadata = build_guardrail_metadata(state, config, "input_guardrail_node")
    guardrail_config = GuardrailConfigUtil.get_config(metadata.get("tenant_id", "default"))

    # Content moderation
    if guardrail_config.content_moderation_enabled:
        try:
            mod_result = await _check_content_moderation(
                user_message,
                metadata,
                guardrail_config.llm_model,
            )
            if (not mod_result.is_safe) and mod_result.confidence > guardrail_config.block_threshold:
                reason = f"{mod_result.violation_category}: {mod_result.explanation}"
                logger.warning(f"[GUARDRAIL] Input blocked (content): {reason}")
                return {
                    "guardrail_status": "input_blocked",
                    "guardrail_reason": reason,
                    "turn_user_response": "I cannot process this request due to content policy restrictions.",
                    "turn_intent": "UNSUPPORTED",
                }
        except Exception as e:
            logger.warning(f"[GUARDRAIL] Content moderation failed: {e}")

    # Prompt injection detection
    if guardrail_config.prompt_injection_enabled:
        try:
            inj_result = await _check_prompt_injection(
                user_message,
                metadata,
                guardrail_config.llm_model,
            )
            if inj_result.is_injection and inj_result.confidence > guardrail_config.block_threshold:
                reason = f"{inj_result.injection_type}: {inj_result.explanation}"
                logger.warning(f"[GUARDRAIL] Input blocked (injection): {reason}")
                return {
                    "guardrail_status": "input_blocked",
                    "guardrail_reason": reason,
                    "turn_user_response": "I cannot comply with that request.",
                    "turn_intent": "UNSUPPORTED",
                }
        except Exception as e:
            logger.warning(f"[GUARDRAIL] Injection detection failed: {e}")

    # PII detection (redact if found)
    validated_message = user_message
    guardrail_reason: Optional[str] = None
    if settings.infra_chat_pii_detection_enabled and guardrail_config.pii_detection_enabled:
        try:
            pii_result = await _check_pii(
                user_message,
                metadata,
                guardrail_config.llm_model,
            )
            if pii_result.contains_pii and pii_result.redacted_text:
                validated_message = pii_result.redacted_text
                guardrail_reason = f"pii_redacted: {', '.join(pii_result.pii_types)}"
        except Exception as e:
            logger.warning(f"[GUARDRAIL] PII detection failed: {e}")

    # All checks passed (or failed open) - continue processing
    updates: Dict[str, Any] = {
        "validated_user_message": validated_message,
    }

    # Keep original user_message intact; only store redacted version for LLM usage
    if validated_message != user_message:
        logger.info("[GUARDRAIL] PII redacted from input message")
        updates["guardrail_reason"] = guardrail_reason
        updates["guardrail_status"] = "pending"

    return updates
