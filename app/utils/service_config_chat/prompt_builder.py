"""
Prompt Builder

Thin orchestrator that uses prompts from separate files.
Single responsibility: assemble prompts with context.
"""
from typing import Dict, Any, List, Optional

from app.utils.service_config_chat.prompts.system_prompt import SYSTEM_PROMPT
from app.utils.service_config_chat.prompts.messages import WELCOME_MESSAGE, REDIRECT_MESSAGE
from app.utils.service_config_chat.prompts.intent_prompt import (
    INTENT_DETECTION_PROMPT,
    SERVICE_EXTRACTION_PROMPT,
)
from app.utils.service_config_chat.prompts.response_prompts import (
    LIST_SERVICES_PROMPT,
    LIST_SERVICES_EMPTY_PROMPT,
    SELECT_SERVICE_FOUND_PROMPT,
    SELECT_SERVICE_MATCHES_PROMPT,
    SELECT_SERVICE_NOT_FOUND_PROMPT,
    SHOW_CONFIG_PROMPT,
    SHOW_CONFIG_EMPTY_PROMPT,
    HELP_PROMPT,
    OUT_OF_SCOPE_PROMPT,
    ASK_PARAMETER_PROMPT,
    ASK_PARAMETER_NO_DATA_PROMPT,
    LIST_PARAMETERS_PROMPT,
    FORM_FILL_PROMPT,
    FORM_FILL_NO_SERVICE_PROMPT,
    DECLINE_PROMPT,
    CLARIFY_AFTER_DECLINE_PROMPT,
)


class PromptBuilder:
    """
    Build prompts for OpenAI LLM.

    Thin orchestrator - prompts stored in separate files.
    """

    # Intent constants
    INTENT_LIST_SERVICES = "list_services"
    INTENT_SELECT_SERVICE = "select_service"
    INTENT_SHOW_CONFIG = "show_config"
    INTENT_FORM_FILL = "form_fill"
    INTENT_DECLINE = "decline"
    INTENT_CLARIFY_AFTER_DECLINE = "clarify_after_decline"
    INTENT_ASK_PARAMETER = "ask_parameter"
    INTENT_LIST_PARAMETERS = "list_parameters"
    INTENT_HELP = "help"
    INTENT_OUT_OF_SCOPE = "out_of_scope"

    def get_system_prompt(self) -> str:
        """Get system prompt."""
        return SYSTEM_PROMPT

    def get_welcome_message(self) -> str:
        """Get welcome message for new sessions."""
        return WELCOME_MESSAGE

    def get_redirect_message(self) -> str:
        """Get redirect message for out-of-scope requests."""
        return REDIRECT_MESSAGE

    def build_intent_detection_prompt(
        self,
        user_message: str,
        current_state: Dict[str, Any],
        history: List = None,
    ) -> str:
        """Build prompt for intent detection."""
        state_context = self._format_state_context(current_state)
        history_context = self._format_history_context(history)
        return INTENT_DETECTION_PROMPT.format(
            state_context=state_context,
            history_context=history_context,
            user_message=user_message,
        )

    def _format_history_context(self, history: List = None) -> str:
        """Format recent conversation history for intent disambiguation."""
        if not history or len(history) == 0:
            return ""

        # Take last 3 messages for context
        recent = history[-3:]
        lines = ["Recent conversation:"]
        for msg in recent:
            role = msg.role if hasattr(msg, 'role') else msg.get('role', 'user')
            message = msg.message if hasattr(msg, 'message') else msg.get('message', '')
            # Truncate long messages
            preview = message[:80] + "..." if len(message) > 80 else message
            lines.append(f"- {role}: {preview}")

        return "\n".join(lines) + "\n"

    def is_parameter_followup(self, history: List = None) -> bool:
        """
        Check if last assistant message was asking for parameter clarification.

        Args:
            history: List of chat messages

        Returns:
            True if user is responding to "Which parameter would you like to know about?"
        """
        if not history:
            return False

        for msg in reversed(history):
            role = msg.role if hasattr(msg, 'role') else msg.get('role', 'agent')
            message = msg.message if hasattr(msg, 'message') else msg.get('message', '')
            if role == "agent":
                return "which parameter" in message.lower()

        return False

    def build_service_extraction_prompt(self, user_message: str) -> str:
        """Build prompt to extract service name from message."""
        return SERVICE_EXTRACTION_PROMPT.format(user_message=user_message)

    def build_response_prompt(
        self,
        intent: str,
        context: Dict[str, Any],
    ) -> str:
        """Build prompt for generating response based on intent."""
        if intent == self.INTENT_LIST_SERVICES:
            return self._build_list_services_prompt(context)
        elif intent == self.INTENT_SELECT_SERVICE:
            return self._build_select_service_prompt(context)
        elif intent == self.INTENT_SHOW_CONFIG:
            return self._build_show_config_prompt(context)
        elif intent == self.INTENT_FORM_FILL:
            return self._build_form_fill_prompt(context)
        elif intent == self.INTENT_DECLINE:
            return DECLINE_PROMPT
        elif intent == self.INTENT_CLARIFY_AFTER_DECLINE:
            return CLARIFY_AFTER_DECLINE_PROMPT.format(
                service_name=context.get("reference_service_name", "the service")
            )
        elif intent == self.INTENT_ASK_PARAMETER:
            return self._build_ask_parameter_prompt(context)
        elif intent == self.INTENT_LIST_PARAMETERS:
            return LIST_PARAMETERS_PROMPT
        elif intent == self.INTENT_HELP:
            return HELP_PROMPT
        else:
            return self._build_out_of_scope_prompt(context)

    def _build_list_services_prompt(self, context: Dict[str, Any]) -> str:
        """Build prompt for listing services."""
        services = context.get("services", [])
        if not services:
            return LIST_SERVICES_EMPTY_PROMPT

        total = len(services)
        return LIST_SERVICES_PROMPT.format(
            total=total,
            infra_context=context.get("infra_context", ""),
            infra_suffix=context.get("infra_suffix", ""),
        )

    def _build_select_service_prompt(self, context: Dict[str, Any]) -> str:
        """Build prompt for service selection."""
        selected = context.get("selected_service")
        matches = context.get("matches", [])

        if selected:
            has_config = context.get("has_config", False)
            config_note = (
                "This service has an existing configuration."
                if has_config
                else "No configuration exists for this service yet."
            )
            return SELECT_SERVICE_FOUND_PROMPT.format(
                service_name=selected.get("name"),
                service_code=selected.get("code"),
                config_note=config_note,
            )

        if matches:
            matches_list = "\n".join([
                f"- {m.get('service_name')} ({m.get('service_code')}) - {int(m.get('similarity_score', 0) * 100)}% match"
                for m in matches[:5]
            ])
            return SELECT_SERVICE_MATCHES_PROMPT.format(matches_list=matches_list)

        return SELECT_SERVICE_NOT_FOUND_PROMPT

    def _build_show_config_prompt(self, context: Dict[str, Any]) -> str:
        """Build prompt for showing reference config."""
        config = context.get("config")
        reference_service_name = context.get("reference_service_name", "the reference service")

        if not config:
            return SHOW_CONFIG_EMPTY_PROMPT.format(service_name=reference_service_name)

        config_summary = context.get("config_summary", str(config))
        source_note = context.get("source_note", "")
        return SHOW_CONFIG_PROMPT.format(
            service_name=reference_service_name,
            source_note=source_note,
            config_summary=config_summary,
        )

    def _build_form_fill_prompt(self, context: Dict[str, Any]) -> str:
        """Build prompt for form fill."""
        if not context.get("config"):
            return FORM_FILL_NO_SERVICE_PROMPT
        return FORM_FILL_PROMPT.format(
            service_name=context.get("reference_service_name", "reference")
        )

    def _build_out_of_scope_prompt(self, context: Dict[str, Any]) -> str:
        """Build prompt for out-of-scope requests."""
        user_message = context.get("user_message", "")
        return OUT_OF_SCOPE_PROMPT.format(user_message=user_message)

    def _build_ask_parameter_prompt(self, context: Dict[str, Any]) -> str:
        """Build prompt for parameter questions."""
        total_services = context.get("total_services", 0)

        if total_services == 0:
            return ASK_PARAMETER_NO_DATA_PROMPT.format(
                parameter_name=context.get("parameter_name", "this parameter"),
                definition=context.get("definition", ""),
            )

        return ASK_PARAMETER_PROMPT.format(
            parameter_name=context.get("parameter_name", ""),
            definition=context.get("definition", ""),
            environment=context.get("environment", ""),
            infra_context=context.get("infra_context", ""),
            infra_suffix=context.get("infra_suffix", ""),
            most_common=context.get("most_common", "varies"),
            total_services=total_services,
            total_configs=context.get("total_configs", 0),
            outlier_note=context.get("outlier_note", ""),
        )

    def _format_state_context(self, state: Dict[str, Any]) -> str:
        """Format current state for prompt."""
        parts = []

        if state.get("is_new_session"):
            parts.append("New session (no previous messages)")
        else:
            parts.append("Existing session")

        if state.get("reference_service"):
            parts.append(f"Reference service selected: {state['reference_service']}")
        else:
            parts.append("No reference service selected")

        if state.get("reference_has_config"):
            parts.append("Reference service has existing config")

        return "\n".join(parts)

    def build_conversation_messages(
        self,
        history: List[Dict[str, Any]],
        current_message: str,
        system_prompt: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Build message list for OpenAI chat completion."""
        messages = [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT}
        ]

        for msg in history[-10:]:
            messages.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", ""),
            })

        messages.append({"role": "user", "content": current_message})
        return messages
