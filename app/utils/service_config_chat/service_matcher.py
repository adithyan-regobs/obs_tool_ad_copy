"""
Service Matcher

Fuzzy matching logic for service names.
Single responsibility: match user input to services.
"""
from difflib import SequenceMatcher
from typing import List, Optional
from dataclasses import dataclass

from app.db.models.services_mst_model import ServicesMstModel


@dataclass
class ServiceMatchResult:
    """Result of fuzzy matching a service."""
    service_code: str
    service_name: str
    service_type: str
    similarity_score: float
    has_existing_config: bool = False


class ServiceMatcher:
    """
    Fuzzy match service names from user input.

    Single responsibility: service name matching.
    Does NOT query database - receives services list as input.
    """

    # Generic words to strip before matching (prevents false positives)
    # Note: "api" intentionally excluded - helps differentiate e.g. "payment-api" vs "payment-background"
    GENERIC_WORDS = {"service", "services", "svc", "app"}

    def __init__(self, threshold: float = 0.55):
        """
        Initialize matcher with similarity threshold.

        Args:
            threshold: Minimum similarity score (0.0 to 1.0) to include in results.
                      Default 0.55 filters out noise while keeping meaningful matches.
        """
        self.threshold = threshold

    def _normalize_for_matching(self, text: str) -> str:
        """
        Remove generic words for better matching.

        Strips common words like 'service', 'api' that would cause
        false positives (e.g., "new service" matching "reward service").

        Args:
            text: Text to normalize

        Returns:
            Normalized text with generic words removed, or original if all words are generic
        """
        words = text.lower().split()
        filtered = [w for w in words if w not in self.GENERIC_WORDS]
        # If all words were generic, return original to avoid empty string
        return " ".join(filtered) if filtered else text.lower()

    def match(
        self,
        query: str,
        services: List[ServicesMstModel],
        limit: int = 5,
    ) -> List[ServiceMatchResult]:
        """
        Fuzzy match query against service names.

        Args:
            query: User input (e.g., "payment", "user-svc")
            services: List of services to match against
            limit: Max results to return (default 5)

        Returns:
            List of ServiceMatchResult sorted by similarity score descending
        """
        if not query or not services:
            return []

        query_lower = query.lower().strip()
        matches = []

        for service in services:
            score = self._calculate_score(query_lower, service)

            if score >= self.threshold:
                matches.append(ServiceMatchResult(
                    service_code=service.code,
                    service_name=service.name,
                    service_type=service.service_type.value,
                    similarity_score=round(score, 2),
                ))

        # Sort by score descending
        matches.sort(key=lambda x: x.similarity_score, reverse=True)

        return matches[:limit]

    def exact_match(
        self,
        query: str,
        services: List[ServicesMstModel],
    ) -> Optional[ServicesMstModel]:
        """
        Find exact match by code or name (case-insensitive).

        Args:
            query: User input
            services: List of services

        Returns:
            ServicesMstModel if exact match found, None otherwise
        """
        if not query or not services:
            return None

        query_lower = query.lower().strip()

        for service in services:
            if (service.code.lower() == query_lower or
                service.name.lower() == query_lower):
                return service

        return None

    def _calculate_score(self, query: str, service: ServicesMstModel) -> float:
        """
        Calculate similarity score between query and service.

        Uses multiple strategies:
        1. Exact substring match (highest priority)
        2. SequenceMatcher ratio on name
        3. SequenceMatcher ratio on code

        Generic words (service, api, etc.) are stripped before comparison
        to prevent false positives like "new service" matching "reward service".

        Args:
            query: Lowercase query string
            service: Service to compare

        Returns:
            Similarity score between 0.0 and 1.0
        """
        name_lower = service.name.lower()
        code_lower = service.code.lower()

        # Normalize by removing generic words for comparison
        query_normalized = self._normalize_for_matching(query)
        name_normalized = self._normalize_for_matching(name_lower)

        # Exact match gets perfect score (check both original and normalized)
        if query == name_lower or query == code_lower:
            return 1.0
        if query_normalized == name_normalized:
            return 1.0

        # Substring match gets high score (use normalized)
        if query_normalized in name_normalized:
            # Score based on how much of the name the query covers
            coverage = len(query_normalized) / len(name_normalized)
            return max(0.8, coverage)

        if query_normalized in code_lower:
            coverage = len(query_normalized) / len(code_lower)
            return max(0.75, coverage)

        # Fall back to sequence matching (use normalized)
        name_score = self._meaningful_match_score(query_normalized, name_normalized)
        code_score = self._meaningful_match_score(query_normalized, code_lower)

        return max(name_score, code_score)

    def _meaningful_match_score(self, query: str, target: str) -> float:
        """
        Calculate score requiring meaningful contiguous matches.

        Penalizes matches that only have scattered single characters.
        Requires at least a 2-character contiguous match for a meaningful score.
        """
        matcher = SequenceMatcher(None, query, target)
        ratio = matcher.ratio()

        # Check for meaningful contiguous matches
        blocks = matcher.get_matching_blocks()
        # Filter out the sentinel block (0, 0, 0) at the end
        real_blocks = [b for b in blocks if b.size > 0]

        if not real_blocks:
            return 0.0

        # Find longest contiguous match
        max_block_size = max(b.size for b in real_blocks)

        # If longest match is only 1 character, heavily penalize
        # (prevents "amal" matching "baa" or "casa" on scattered 'a's)
        if max_block_size == 1:
            return ratio * 0.3  # Reduce score by 70%

        # If longest match is 2 characters, slight penalty for longer queries
        if max_block_size == 2 and len(query) > 3:
            return ratio * 0.7

        return ratio

    def match_from_dicts(
        self,
        query: str,
        services: List[dict],
        limit: int = 5,
    ) -> List[ServiceMatchResult]:
        """
        Fuzzy match query against service dicts (from get_services_with_config_status).

        Args:
            query: User input (e.g., "payment", "user-svc")
            services: List of service dicts with service_code, service_name, service_type, has_existing_config
            limit: Max results to return (default 5)

        Returns:
            List of ServiceMatchResult sorted by similarity score descending
        """
        if not query or not services:
            return []

        query_lower = query.lower().strip()
        matches = []

        for service in services:
            score = self._calculate_score_from_dict(query_lower, service)

            if score >= self.threshold:
                matches.append(ServiceMatchResult(
                    service_code=service["service_code"],
                    service_name=service["service_name"],
                    service_type=service["service_type"],
                    similarity_score=round(score, 2),
                    has_existing_config=service.get("has_existing_config", False),
                ))

        # Sort by score descending
        matches.sort(key=lambda x: x.similarity_score, reverse=True)

        return matches[:limit]

    def _calculate_score_from_dict(self, query: str, service: dict) -> float:
        """
        Calculate similarity score between query and service dict.

        Generic words (service, api, etc.) are stripped before comparison
        to prevent false positives like "new service" matching "reward service".

        Args:
            query: Lowercase query string
            service: Service dict with service_name and service_code

        Returns:
            Similarity score between 0.0 and 1.0
        """
        name_lower = service["service_name"].lower()
        code_lower = service["service_code"].lower()

        # Normalize by removing generic words for comparison
        query_normalized = self._normalize_for_matching(query)
        name_normalized = self._normalize_for_matching(name_lower)

        # Exact match gets perfect score (check both original and normalized)
        if query == name_lower or query == code_lower:
            return 1.0
        if query_normalized == name_normalized:
            return 1.0

        # Substring match gets high score (use normalized)
        if query_normalized in name_normalized:
            coverage = len(query_normalized) / len(name_normalized)
            return max(0.8, coverage)

        if query_normalized in code_lower:
            coverage = len(query_normalized) / len(code_lower)
            return max(0.75, coverage)

        # Fall back to sequence matching (use normalized)
        name_score = self._meaningful_match_score(query_normalized, name_normalized)
        code_score = self._meaningful_match_score(query_normalized, code_lower)

        return max(name_score, code_score)

    def enrich_with_config_status(
        self,
        matches: List[ServiceMatchResult],
        services_with_config: set,
    ) -> List[ServiceMatchResult]:
        """
        Add has_existing_config flag to matches.

        Args:
            matches: List of match results
            services_with_config: Set of service codes that have existing config

        Returns:
            Same matches with has_existing_config populated
        """
        for match in matches:
            match.has_existing_config = match.service_code in services_with_config

        return matches
