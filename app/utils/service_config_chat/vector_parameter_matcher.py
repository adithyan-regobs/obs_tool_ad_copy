"""
Vector Parameter Matcher for semantic parameter matching.

Implements hybrid approach: hardcoded aliases first (fast, 100% accurate),
vector similarity search fallback (handles novel queries like "heap size" -> "xmx").

Includes TTL-based caching for vector search results to reduce latency.
"""
import asyncio
import logging
import time
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from enum import Enum

from app.core.config import settings
from app.integrations.qdrant_integration import QdrantIntegration
from app.integrations.openai_integration import OpenAIIntegration
from app.services.langfuse_service import langfuse_service
from app.utils.service_config_chat.prompts.parameter_definitions import (
    get_canonical_parameter,
    get_parameter_definition,
    PARAMETER_DEFINITIONS,
)
from app.utils.service_config_chat.cache import get_parameter_cache

logger = logging.getLogger(__name__)


class MatchConfidence(Enum):
    """
    Confidence level of parameter match.

    Used to determine how to handle the match in the service layer:
    - EXACT: Direct match, proceed with full confidence
    - HIGH: Vector match with high score, proceed with confidence
    - UNCERTAIN: Vector match with medium score, may need user confirmation
    - NO_MATCH: No match found, ask user for clarification
    """

    EXACT = "exact"  # Hardcoded alias match (score=1.0)
    HIGH = "high"  # Vector score >= 0.85
    UNCERTAIN = "uncertain"  # Vector score 0.70-0.85
    NO_MATCH = "no_match"  # Vector score < 0.70 or no result


@dataclass
class ParameterMatch:
    """
    Result of parameter matching.

    Attributes:
        canonical_name: The canonical parameter name (e.g., "xmx", "memory")
        confidence: Confidence level of the match
        score: Similarity score (1.0 for exact, vector score otherwise)
        definition: One-liner definition of the parameter
        alternatives: List of alternative matches for uncertain cases
    """

    canonical_name: Optional[str]
    confidence: MatchConfidence
    score: float
    definition: Optional[str]
    alternatives: List[Dict[str, Any]]


class VectorParameterMatcher:
    """
    Hybrid parameter matcher using hardcoded aliases + vector search.

    Match priority:
    1. Exact match in PARAMETER_DEFINITIONS keys
    2. Alias lookup in PARAMETER_ALIASES (via get_canonical_parameter)
    3. Vector similarity search in Qdrant

    This ensures:
    - Known aliases always work with 100% accuracy (fast, deterministic)
    - Novel queries are handled semantically (flexible, handles variations)
    - Graceful degradation when Qdrant is unavailable
    """

    def __init__(self):
        """Initialize matcher with thresholds from settings."""
        self._collection_name = settings.qdrant_collection_name
        self._auto_accept_threshold = settings.vector_match_auto_accept
        self._uncertain_threshold = settings.vector_match_uncertain

    async def match_parameter(self, query: str) -> ParameterMatch:
        """
        Match user query to canonical parameter name using hybrid approach.

        Priority:
        1. Check exact match in PARAMETER_DEFINITIONS keys
        2. Check alias lookup in PARAMETER_ALIASES
        3. Fall back to vector similarity search

        Args:
            query: User's parameter query (e.g., "heap size", "memory limit", "ram")

        Returns:
            ParameterMatch with canonical name, confidence, score, and alternatives
        """
        start_time = time.perf_counter()
        match_path = "no_match"
        result = None

        try:
            if not query:
                result = ParameterMatch(
                    canonical_name=None,
                    confidence=MatchConfidence.NO_MATCH,
                    score=0.0,
                    definition=None,
                    alternatives=[],
                )
                return result

            normalized_query = query.lower().strip()

            # Step 1: Check exact match in PARAMETER_DEFINITIONS
            if normalized_query in PARAMETER_DEFINITIONS:
                logger.debug(f"Exact match found for '{normalized_query}'")
                match_path = "exact"
                result = ParameterMatch(
                    canonical_name=normalized_query,
                    confidence=MatchConfidence.EXACT,
                    score=1.0,
                    definition=PARAMETER_DEFINITIONS[normalized_query],
                    alternatives=[],
                )
                return result

            # Step 2: Check alias lookup
            canonical = get_canonical_parameter(normalized_query)
            if canonical != normalized_query and canonical in PARAMETER_DEFINITIONS:
                logger.debug(f"Alias match: '{normalized_query}' -> '{canonical}'")
                match_path = "alias"
                result = ParameterMatch(
                    canonical_name=canonical,
                    confidence=MatchConfidence.EXACT,
                    score=1.0,
                    definition=PARAMETER_DEFINITIONS[canonical],
                    alternatives=[],
                )
                return result

            # Step 3: Vector similarity search (fallback)
            logger.debug(f"No alias match for '{normalized_query}', trying vector search")
            match_path = "vector"
            result = await self._vector_search(normalized_query)
            if result.confidence == MatchConfidence.NO_MATCH:
                match_path = "no_match"
            return result
        finally:
            # Fire-and-forget: Log parameter match
            latency_ms = (time.perf_counter() - start_time) * 1000
            if result:
                asyncio.create_task(langfuse_service.log_parameter_match(
                    query=query,
                    match_path=match_path,
                    canonical_name=result.canonical_name,
                    confidence=result.confidence.value,
                    score=result.score,
                    latency_ms=latency_ms,
                    alternatives_count=len(result.alternatives)
                ))

    async def _vector_search(self, query: str) -> ParameterMatch:
        """
        Perform vector similarity search in Qdrant with caching.

        Uses TTL-based cache to avoid repeated embedding + vector search
        for the same query within the cache window (10 minutes).

        Args:
            query: Normalized query string

        Returns:
            ParameterMatch based on vector search results
        """
        # Check cache first
        cache = get_parameter_cache()
        cache_key = f"vector:{query}"
        cached_result = await cache.get(cache_key)
        if cached_result is not None:
            logger.debug(f"Cache hit for vector search: '{query}'")
            return cached_result

        # Check Qdrant availability
        if not await QdrantIntegration.health_check():
            logger.warning("Qdrant unavailable, returning no match")
            return ParameterMatch(
                canonical_name=None,
                confidence=MatchConfidence.NO_MATCH,
                score=0.0,
                definition=None,
                alternatives=[],
            )

        # Generate query embedding
        query_embedding = await OpenAIIntegration.embed_text(query)
        if not query_embedding:
            logger.error(f"Failed to generate embedding for query: '{query}'")
            return ParameterMatch(
                canonical_name=None,
                confidence=MatchConfidence.NO_MATCH,
                score=0.0,
                definition=None,
                alternatives=[],
            )

        # Search Qdrant with minimum threshold
        results = await QdrantIntegration.search(
            collection_name=self._collection_name,
            query_vector=query_embedding,
            limit=5,
            score_threshold=self._uncertain_threshold,
        )

        if not results:
            logger.debug(f"No vector matches found for '{query}'")
            result = ParameterMatch(
                canonical_name=None,
                confidence=MatchConfidence.NO_MATCH,
                score=0.0,
                definition=None,
                alternatives=[],
            )
            # Cache no-match results for shorter time (2 min)
            await cache.set(cache_key, result, ttl=120)
            return result

        # Process top result
        top_result = results[0]
        top_score = top_result["score"]
        canonical_name = top_result["canonical_name"]

        # Determine confidence level based on score
        if top_score >= self._auto_accept_threshold:
            confidence = MatchConfidence.HIGH
            logger.info(
                f"Vector match (HIGH): '{query}' -> '{canonical_name}' (score={top_score:.3f})"
            )
        else:
            confidence = MatchConfidence.UNCERTAIN
            logger.info(
                f"Vector match (UNCERTAIN): '{query}' -> '{canonical_name}' (score={top_score:.3f})"
            )

        # Get definition from canonical name
        definition = PARAMETER_DEFINITIONS.get(canonical_name)

        # Build alternatives list for uncertain matches
        alternatives = []
        if confidence == MatchConfidence.UNCERTAIN:
            alternatives = [
                {
                    "canonical_name": r["canonical_name"],
                    "score": r["score"],
                    "category": r["payload"].get("category"),
                }
                for r in results[1:4]  # Top 3 alternatives
                if r["score"] >= self._uncertain_threshold
            ]

        result = ParameterMatch(
            canonical_name=canonical_name,
            confidence=confidence,
            score=top_score,
            definition=definition,
            alternatives=alternatives,
        )

        # Cache successful matches
        await cache.set(cache_key, result)
        return result

    async def is_qdrant_available(self) -> bool:
        """
        Check if Qdrant is available for vector search.

        Returns:
            True if Qdrant is reachable, False otherwise
        """
        return await QdrantIntegration.health_check()

    async def is_collection_ready(self) -> bool:
        """
        Check if the parameter collection exists and is ready.

        Returns:
            True if collection exists, False otherwise
        """
        return await QdrantIntegration.collection_exists(self._collection_name)
