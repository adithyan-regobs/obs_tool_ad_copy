"""
Service Resolver for Tools.

Thin orchestrator that delegates to ServiceMatcher.
Single responsibility: Resolve service names with confidence threshold.
"""
import logging
from typing import List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

from app.services.service_config_agent.tools.base import ServiceResolutionResult


# Confidence threshold for auto-resolution
AUTO_RESOLVE_THRESHOLD = 0.8

# If multiple matches are within this score delta of each other, ask for clarification
AMBIGUITY_DELTA = 0.1


@runtime_checkable
class ServiceMatcherProtocol(Protocol):
    """Contract for service matching."""

    def exact_match(self, query: str, services: List) -> Optional[object]:
        """Find exact match by code or name."""
        ...

    def match_from_dicts(
        self, query: str, services: List[dict], limit: int = 5
    ) -> List:
        """Fuzzy match against service dicts."""
        ...


class ServiceResolver:
    """
    Thin orchestrator for service name resolution.

    Delegates to ServiceMatcher for actual matching logic.
    Single responsibility: orchestrate resolution with confidence threshold.
    """

    def __init__(
        self,
        matcher: ServiceMatcherProtocol,
        threshold: float = AUTO_RESOLVE_THRESHOLD,
    ):
        """
        Initialize resolver with matcher.

        Args:
            matcher: ServiceMatcher instance for fuzzy matching
            threshold: Confidence threshold for auto-resolution (default 0.8)
        """
        self._matcher = matcher
        self._threshold = threshold

    def resolve(
        self,
        query: str,
        services: List[dict],
    ) -> ServiceResolutionResult:
        """
        Resolve service name to code.

        Strategy:
        1. Exact match (code or name) → immediate success
        2. Fuzzy match → check confidence threshold
        3. Below threshold → return suggestions

        Args:
            query: User input (service name, may contain typos)
            services: List of service dicts with service_code, service_name, service_type

        Returns:
            ServiceResolutionResult with resolution status and data
        """
        if not query or not services:
            return ServiceResolutionResult(
                resolved=False,
                error="No query or services provided",
            )

        # Step 1: Try exact match first (fast path)
        for service in services:
            if self._is_exact_match(query, service):
                return ServiceResolutionResult(
                    resolved=True,
                    service_code=service["service_code"],
                    service_name=service["service_name"],
                    confidence=1.0,
                )

        # Step 2: Fuzzy match
        matches = self._matcher.match_from_dicts(query, services, limit=5)

        # Log fuzzy match results
        if matches:
            logger.info(
                f"[SERVICE_RESOLVER] Fuzzy matches for '{query}': "
                f"{[(m.service_name, f'{m.similarity_score:.3f}') for m in matches]}"
            )
        else:
            logger.info(f"[SERVICE_RESOLVER] No fuzzy matches found for '{query}'")

        if not matches:
            return ServiceResolutionResult(
                resolved=False,
                error=f"No services matching '{query}' found",
            )

        top_match = matches[0]

        # Step 3: Check for ambiguous matches (multiple services with similar scores)
        # This handles cases like "ninto" matching ninto-104, ninto-105, ninto-106
        if top_match.similarity_score >= self._threshold:
            # Find all matches within AMBIGUITY_DELTA of top score
            close_matches = [
                m for m in matches
                if top_match.similarity_score - m.similarity_score <= AMBIGUITY_DELTA
            ]

            if len(close_matches) > 1:
                # Multiple similar matches - ask for clarification
                # Include full match info for clickable buttons
                suggestions = [m.service_name for m in close_matches[:5]]
                matched_services = [
                    {
                        "service_code": m.service_code,
                        "service_name": m.service_name,
                        "service_type": m.service_type,
                        "similarity_score": m.similarity_score,
                    }
                    for m in close_matches[:5]
                ]
                return ServiceResolutionResult(
                    resolved=False,
                    service_code=top_match.service_code,
                    service_name=top_match.service_name,
                    confidence=top_match.similarity_score,
                    suggestions=suggestions,
                    matched_services=matched_services,
                    error=f"Multiple services match '{query}'. Please specify: {', '.join(suggestions)}",
                )

            # Single clear match
            return ServiceResolutionResult(
                resolved=True,
                service_code=top_match.service_code,
                service_name=top_match.service_name,
                confidence=top_match.similarity_score,
            )

        # Below threshold - return suggestions
        suggestions = [m.service_name for m in matches[:3]]
        return ServiceResolutionResult(
            resolved=False,
            service_code=top_match.service_code,
            service_name=top_match.service_name,
            confidence=top_match.similarity_score,
            suggestions=suggestions,
            error=f"Low confidence match. Did you mean: {', '.join(suggestions)}?",
        )

    def _is_exact_match(self, query: str, service: dict) -> bool:
        """Check for exact match (case-insensitive)."""
        query_lower = query.lower().strip()
        return (
            service["service_code"].lower() == query_lower
            or service["service_name"].lower() == query_lower
        )
