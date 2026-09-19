"""
Intent Detector for Service Config Chat.

Single responsibility: detect user intent from message.
Encapsulates LLM calls, pre-checks, and validation.
"""

import asyncio
import re
import time
from typing import Dict, Any, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.services.langfuse_service import langfuse_service
from app.utils.service_config_chat.prompt_builder import PromptBuilder


class IntentDetector:
    """
    Detects user intent from messages.

    Responsibilities:
    - Pre-checks for form fill confirmations
    - LLM-based intent classification
    - Intent validation
    """

    def __init__(self, llm):
        """
        Initialize with LLM client.

        Args:
            llm: LangChain chat client for intent detection
        """
        self.llm = llm
        self.prompts = PromptBuilder()

    async def detect(
        self,
        message: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]] = None,
    ) -> str:
        """
        Detect intent from user message.

        Args:
            message: User's message
            context: State context (reference_service, etc.)
            history: Conversation history

        Returns:
            Detected intent string
        """
        history = history or []

        # Pre-check 1: pending field fill with custom value
        if self._has_pending_fill_with_value(message, history):
            return self.prompts.INTENT_FORM_FILL

        # Pre-check 2: parameter clarification follow-up
        if self.prompts.is_parameter_followup(history):
            return self.prompts.INTENT_ASK_PARAMETER

        # LLM-based detection
        return await self._detect_with_llm(message, context, history)

    async def _detect_with_llm(
        self,
        message: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]],
    ) -> str:
        """Use LLM to classify intent."""
        prompt = self.prompts.build_intent_detection_prompt(message, context, history)

        messages = [
            SystemMessage(content="You are an intent classifier. Respond with only the intent name."),
            HumanMessage(content=prompt),
        ]

        start_time = time.perf_counter()
        response = await self.llm.ainvoke(messages)
        intent = response.content.strip().lower()
        latency_ms = (time.perf_counter() - start_time) * 1000

        # Fire-and-forget: Log LLM call
        asyncio.create_task(langfuse_service.log_llm_call(
            call_type="intent_detection",
            input_text=message[:200],
            output_text=intent,
            latency_ms=latency_ms
        ))

        # Validate intent
        return self._validate_intent(intent)

    def _validate_intent(self, intent: str) -> str:
        """Validate and normalize intent."""
        valid_intents = {
            self.prompts.INTENT_LIST_SERVICES,
            self.prompts.INTENT_SELECT_SERVICE,
            self.prompts.INTENT_SHOW_CONFIG,
            self.prompts.INTENT_FORM_FILL,
            self.prompts.INTENT_DECLINE,
            self.prompts.INTENT_ASK_PARAMETER,
            self.prompts.INTENT_LIST_PARAMETERS,
            self.prompts.INTENT_HELP,
            self.prompts.INTENT_OUT_OF_SCOPE,
        }

        if intent not in valid_intents:
            return self.prompts.INTENT_OUT_OF_SCOPE

        return intent

    def _has_pending_fill_with_value(
        self,
        message: str,
        history: List[Dict[str, str]],
    ) -> bool:
        """
        Check if there's a pending field fill offer AND message contains a custom value.

        Catches: "no true", "no, use 7", "no fill with 1024"
        """
        pending = self._get_pending_field_from_history(history)
        if not pending:
            return False

        cleaned = message.lower().strip()

        # Remove "no" prefix variations
        value_part = cleaned
        for prefix in ["no,", "no.", "no ", "nope,", "nope.", "nope "]:
            if cleaned.startswith(prefix):
                value_part = cleaned[len(prefix):].strip()
                break

        # Check for boolean values
        if value_part in {'true', 'false', 'enable', 'disable', 'enabled', 'disabled', 'on', 'off'}:
            return True

        # Check for "fill with X" or "use X" patterns
        if re.search(r'(?:fill\s*(?:with)?|use|set\s*(?:it)?\s*to)\s*\S+', value_part):
            return True

        # Check for standalone number
        if re.match(r'^\d+$', value_part):
            return True

        return False

    def _get_pending_field_from_history(
        self,
        history: List[Dict[str, str]],
    ) -> Optional[Dict[str, str]]:
        """Extract pending field fill from history."""
        if not history:
            return None

        for msg in reversed(history):
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role == "agent":
                message = msg.get('message', '') if isinstance(msg, dict) else getattr(msg, 'message', '')
                match = re.search(
                    r"Would you like me to fill '([^']+)' with value '([^']+)'\?",
                    message
                )
                if match:
                    return {"field": match.group(1), "value": match.group(2)}
                break

        return None
