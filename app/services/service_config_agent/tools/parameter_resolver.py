"""
Parameter Resolver for Tools.

Thin orchestrator that delegates to ParameterHelper.
Single responsibility: Resolve parameter names with confidence threshold.
"""

from typing import Protocol, runtime_checkable, Tuple, Optional

from app.services.service_config_agent.tools.base import ParameterResolutionResult


# Confidence threshold for auto-resolution
# Aligned with legacy UNCERTAIN_THRESHOLD (0.7)
AUTO_RESOLVE_THRESHOLD = 0.7


@runtime_checkable
class ParameterHelperProtocol(Protocol):
    """Contract for parameter matching."""

    async def match_parameter_hybrid(
        self, name: str
    ) -> Tuple[Optional[str], object, float]:
        """Hybrid parameter matching: aliases first, vector search fallback."""
        ...

    def get_definition(self, name: str) -> Optional[str]:
        """Get one-liner definition for parameter."""
        ...


class ParameterResolver:
    """
    Thin orchestrator for parameter name resolution.

    Delegates to ParameterHelper for actual matching logic.
    Single responsibility: orchestrate resolution with confidence threshold.
    """

    def __init__(
        self,
        helper: ParameterHelperProtocol,
        threshold: float = AUTO_RESOLVE_THRESHOLD,
    ):
        """
        Initialize resolver with helper.

        Args:
            helper: ParameterHelper instance for hybrid matching
            threshold: Confidence threshold for auto-resolution (default 0.8)
        """
        self._helper = helper
        self._threshold = threshold

    async def resolve(
        self,
        query: str,
    ) -> ParameterResolutionResult:
        """
        Resolve parameter name to canonical form.

        Strategy:
        1. Hardcoded alias lookup (fast, 100% accurate)
        2. Vector similarity search (flexible, handles novel queries)
        3. Check confidence threshold for auto-resolution

        Args:
            query: User input (parameter name, may contain aliases or typos)

        Returns:
            ParameterResolutionResult with resolution status and data
        """
        if not query:
            return ParameterResolutionResult(
                resolved=False,
                error="No parameter query provided",
            )

        # Delegate to hybrid matching
        canonical_name, confidence, score = await self._helper.match_parameter_hybrid(
            query
        )

        # Convert confidence enum to string
        confidence_str = confidence.value if hasattr(confidence, "value") else str(confidence)

        if canonical_name is None:
            return ParameterResolutionResult(
                resolved=False,
                confidence=confidence_str,
                score=score,
                error=f"No parameter matching '{query}' found",
            )

        # Check confidence threshold
        if score >= self._threshold:
            return ParameterResolutionResult(
                resolved=True,
                canonical_name=canonical_name,
                confidence=confidence_str,
                score=score,
            )

        # Below threshold - still return the match but mark as low confidence
        return ParameterResolutionResult(
            resolved=False,
            canonical_name=canonical_name,
            confidence=confidence_str,
            score=score,
            suggestions=[canonical_name],
            error=f"Low confidence match for '{query}'. Did you mean '{canonical_name}'?",
        )
