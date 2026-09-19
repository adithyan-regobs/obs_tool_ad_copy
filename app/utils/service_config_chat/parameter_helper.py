"""
Parameter Helper for Service Config Assistant.

Single responsibility: Extract and normalize parameter names from user messages.
Supports hybrid matching: hardcoded aliases first, vector search fallback.
"""
from typing import Optional, Tuple, List
from langchain_core.messages import HumanMessage, SystemMessage

from app.core.enum import EnvironmentEnum

from app.utils.service_config_chat.prompts.intent_prompt import PARAMETER_EXTRACTION_PROMPT
from app.utils.service_config_chat.prompts.parameter_definitions import (
    get_canonical_parameter,
    get_parameter_definition,
)
from app.utils.service_config_chat.vector_parameter_matcher import (
    VectorParameterMatcher,
    ParameterMatch,
    MatchConfidence,
)


class ParameterHelper:
    """
    Helper for parameter extraction and normalization.

    Responsibilities:
    - Extract parameter name from user message using LLM
    - Normalize parameter names to canonical form (hardcoded aliases)
    - Hybrid matching: aliases first, vector search fallback
    - Provide parameter definitions
    """

    def __init__(self):
        """Initialize helper with lazy-loaded vector matcher."""
        self._vector_matcher: Optional[VectorParameterMatcher] = None

    @property
    def vector_matcher(self) -> VectorParameterMatcher:
        """Lazy-load vector matcher on first access."""
        if self._vector_matcher is None:
            self._vector_matcher = VectorParameterMatcher()
        return self._vector_matcher

    async def extract_parameter_name(self, llm, message: str) -> Optional[str]:
        """
        Extract parameter name from user message using LLM.

        Args:
            llm: LangChain LLM instance
            message: User message

        Returns:
            Extracted parameter name or None
        """
        prompt = PARAMETER_EXTRACTION_PROMPT.format(user_message=message)

        messages = [
            SystemMessage(content="You extract config parameter names. Respond with only the parameter name or NONE."),
            HumanMessage(content=prompt),
        ]

        response = await llm.ainvoke(messages)
        result = response.content.strip().lower()

        if result == "none" or not result:
            return None

        return result

    def normalize_parameter_name(self, name: str) -> str:
        """
        Normalize parameter name to canonical form.

        Args:
            name: Parameter name or alias

        Returns:
            Canonical parameter name
        """
        return get_canonical_parameter(name)

    def get_definition(self, name: str) -> Optional[str]:
        """
        Get one-liner definition for parameter.

        Args:
            name: Parameter name (will be canonicalized)

        Returns:
            Definition string or None
        """
        return get_parameter_definition(name)

    def format_outliers(self, outliers: list) -> str:
        """
        Format outlier services for response prompt.

        Args:
            outliers: List of {service, value} dicts

        Returns:
            Formatted string or empty
        """
        if not outliers:
            return ""

        # Show top 2 outliers max
        top_outliers = outliers[:2]
        parts = [f"{o['service']} uses {o['value']}" for o in top_outliers]
        return "Notable: " + ", ".join(parts)

    async def match_parameter_hybrid(
        self, name: str
    ) -> Tuple[Optional[str], MatchConfidence, float]:
        """
        Hybrid parameter matching: aliases first, vector search fallback.

        Priority:
        1. Try hardcoded alias lookup (fast, 100% accurate)
        2. Fall back to vector similarity search (flexible, handles novel queries)

        Args:
            name: Parameter name or query from user

        Returns:
            Tuple of (canonical_name, confidence, score)
            - canonical_name: The matched parameter name or None
            - confidence: MatchConfidence enum value
            - score: Similarity score (1.0 for exact, vector score otherwise)
        """
        if not name:
            return (None, MatchConfidence.NO_MATCH, 0.0)

        # Step 1: Try hardcoded alias lookup
        canonical = get_canonical_parameter(name)
        definition = get_parameter_definition(canonical)

        if definition:
            # Found via hardcoded lookup - 100% confident
            return (canonical, MatchConfidence.EXACT, 1.0)

        # Step 2: Fall back to vector search
        match = await self.vector_matcher.match_parameter(name)
        return (match.canonical_name, match.confidence, match.score)

    def mentions_other_environment(
        self,
        message: str,
        current_env: EnvironmentEnum,
    ) -> bool:
        """
        Check if user is asking about a different environment.

        Args:
            message: User message
            current_env: Current environment from context

        Returns:
            True if user mentions an environment different from current
        """
        msg_lower = message.lower()

        env_keywords = {
            EnvironmentEnum.dev: ["dev", "development"],
            EnvironmentEnum.staging: ["stage", "staging"],
            EnvironmentEnum.qa: ["qa"],
            EnvironmentEnum.prod: ["prod", "production"],
        }

        for env, keywords in env_keywords.items():
            if env != current_env:
                for keyword in keywords:
                    if keyword in msg_lower:
                        return True

        return False

    def resolve_disambiguation(
        self,
        message: str,
        history: List = None,
    ) -> Optional[str]:
        """
        Resolve disambiguation follow-up from history.

        Checks if last assistant message was a disambiguation question
        and maps user's response to the appropriate parameter.

        Args:
            message: User's current message
            history: List of chat messages

        Returns:
            Parameter name if disambiguation resolved, None otherwise.
        """
        if not history:
            return None

        # Get last assistant message
        last_assistant = None
        for msg in reversed(history):
            role = msg.role if hasattr(msg, 'role') else msg.get('role', 'agent')
            msg_content = msg.message if hasattr(msg, 'message') else msg.get('message', '')
            if role == "agent":
                last_assistant = msg_content
                break

        if not last_assistant:
            return None

        msg_lower = message.lower().strip()

        # Check for parameter question follow-up
        if "which parameter" in last_assistant.lower():
            return msg_lower

        # Check for scaling disambiguation
        if "which scaling" in last_assistant.lower():
            if msg_lower in ("task", "task scaling", "autoscaling"):
                return "enable_autoscaling"
            if msg_lower in ("http", "http scaling", "request", "request scaling"):
                return "http_scaling_enabled"

        # Check for sidecar disambiguation
        if "which sidecar" in last_assistant.lower():
            if msg_lower in ("datadog", "dd"):
                return "enable_datadog_sidecar"
            if msg_lower in ("otel", "opentelemetry"):
                return "enable_otel_sidecar"

        return None
