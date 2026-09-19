"""
Output guardrail node.

Runs safety checks on the final response text and redacts/blocks if needed.
"""
import logging
from typing import Dict, Any, Optional

from app.infra_chat_agent.chat_state import ChatState
from app.core.config import settings
from app.infra_chat_agent.guardrails.guardrail_config import (
    ContentModerationResult,
    PIIResult,
    GuardrailConfigUtil,
)
from app.infra_chat_agent.guardrails.guardrail_helper import (
    build_guardrail_metadata,
    build_invoke_config,
    run_guardrail_check,
    create_pii_detector,
    collect_pii_exempt_values,
    mask_allowed_values,
    restore_allowed_values,
)

logger = logging.getLogger(__name__)


async def _check_content_moderation(
    text: str,
    metadata: Dict[str, Any],
    model_name: str,
) -> ContentModerationResult:
    system_prompt = (
        "You are a content moderation system for an infrastructure assistant. "
        "Evaluate whether the response contains unsafe or policy-violating content. "
        "Be strict but avoid false positives."
    )
    return await run_guardrail_check(
        ContentModerationResult,
        system_prompt,
        text,
        "guardrail_output_content",
        metadata,
        model_name,
    )


async def _check_pii(
    text: str,
    metadata: Dict[str, Any],
    model_name: str,
) -> PIIResult:
    detector = create_pii_detector(model_name)
    invoke_config = build_invoke_config("guardrail_output_pii", metadata)
    return await detector.ainvoke({"text": text}, config=invoke_config)


async def output_guardrail_node(state: ChatState, config) -> ChatState:
    """LLM-based output guardrails for infra-chat."""
    if not settings.infra_chat_guardrails_enabled:
        return {}

    response_text = (state.get("turn_user_response") or "").strip()
    if not response_text:
        return {}

    metadata = build_guardrail_metadata(state, config, "output_guardrail_node")
    guardrail_config = GuardrailConfigUtil.get_config(metadata.get("tenant_id", "default"))

    # Content moderation
    if guardrail_config.content_moderation_enabled:
        try:
            mod_result = await _check_content_moderation(
                response_text,
                metadata,
                guardrail_config.llm_model,
            )
            if (not mod_result.is_safe) and mod_result.confidence > guardrail_config.block_threshold:
                reason = f"{mod_result.violation_category}: {mod_result.explanation}"
                logger.warning(f"[GUARDRAIL] Output blocked (content): {reason}")
                return {
                    "guardrail_status": "output_blocked",
                    "guardrail_reason": reason,
                    "turn_user_response": "I cannot provide that response due to content policy restrictions.",
                    "turn_intent": "UNSUPPORTED",
                }
        except Exception as e:
            logger.warning(f"[GUARDRAIL] Output moderation failed: {e}")

    # PII detection (redact if found)
    updated_text = response_text
    guardrail_reason: Optional[str] = None
    if settings.infra_chat_pii_detection_enabled and guardrail_config.pii_detection_enabled:
        allowed_value_map = collect_pii_exempt_values(state)
        allowed_values = [value for values in allowed_value_map.values() for value in values]
        masked_text = response_text
        token_map: Dict[str, str] = {}
        if allowed_values:
            masked_text, token_map = mask_allowed_values(response_text, allowed_values)
            if token_map:
                logger.info(
                    "[GUARDRAIL] Output allowlist masking applied for params: %s",
                    list(allowed_value_map.keys()),
                )
        try:
            pii_result = await _check_pii(
                masked_text,
                metadata,
                guardrail_config.llm_model,
            )
            if pii_result.contains_pii and pii_result.redacted_text:
                updated_text = pii_result.redacted_text
                if token_map:
                    updated_text = restore_allowed_values(updated_text, token_map)
                guardrail_reason = f"pii_redacted: {', '.join(pii_result.pii_types)}"
        except Exception as e:
            logger.warning(f"[GUARDRAIL] Output PII detection failed: {e}")

    # Apply redaction if needed
    if updated_text != response_text:
        logger.info("[GUARDRAIL] PII redacted from output message")
        return {
            "turn_user_response": updated_text,
            "guardrail_reason": guardrail_reason,
            "guardrail_status": "pending",
        }

    return {}
