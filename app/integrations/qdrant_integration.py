"""
Qdrant Vector Database Integration.

Provides async client wrapper for Qdrant operations with lazy initialization,
connection management, and graceful error handling.
"""
import asyncio
import logging
import time
from typing import Optional, List, Dict, Any

try:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as qdrant_models
    from qdrant_client.http.exceptions import UnexpectedResponse
    QDRANT_AVAILABLE = True
except ImportError:
    AsyncQdrantClient = None
    qdrant_models = None
    UnexpectedResponse = None
    QDRANT_AVAILABLE = False

from app.core.config import settings
from app.services.langfuse_service import langfuse_service

logger = logging.getLogger(__name__)


class QdrantIntegration:
    """
    Singleton wrapper for AsyncQdrantClient with lazy initialization.

    Provides:
    - Async client initialization with settings
    - Health check for connectivity verification
    - Collection management (create, check existence)
    - Vector search with score threshold
    - Graceful error handling (returns empty on failures)
    """

    _client: Optional[AsyncQdrantClient] = None
    _initialized: bool = False

    @classmethod
    async def get_client_async(cls) -> Optional[AsyncQdrantClient]:
        """
        Get or create async Qdrant client instance.

        Returns:
            AsyncQdrantClient instance or None if disabled or initialization failed
        """
        if not settings.qdrant_enabled or not QDRANT_AVAILABLE:
            return None
        if not cls._initialized:
            await cls._initialize_client()
        return cls._client

    @classmethod
    async def _initialize_client(cls) -> None:
        """Initialize AsyncQdrantClient with settings from config."""
        try:
            # Prefer URL over host:port (supports ALB path prefixes)
            if settings.qdrant_url:
                cls._client = AsyncQdrantClient(
                    url=settings.qdrant_url,
                    api_key=settings.qdrant_api_key or None,
                    timeout=settings.qdrant_timeout,
                    prefer_grpc=False,  # Use REST API for ALB compatibility
                )
                connection_info = settings.qdrant_url
            else:
                cls._client = AsyncQdrantClient(
                    host=settings.qdrant_host,
                    port=settings.qdrant_port,
                    timeout=settings.qdrant_timeout,
                )
                connection_info = f"{settings.qdrant_host}:{settings.qdrant_port}"

            cls._initialized = True
            logger.info(f"AsyncQdrantClient initialized: {connection_info}")
        except Exception as e:
            logger.error(f"Failed to initialize Qdrant client: {e}")
            cls._client = None
            cls._initialized = True  # Mark as attempted to avoid repeated failures

    @classmethod
    def reset_client(cls) -> None:
        """Reset client for re-initialization (useful for testing)."""
        cls._client = None
        cls._initialized = False

    @classmethod
    async def health_check(cls) -> bool:
        """
        Check if Qdrant is reachable.

        Returns:
            True if Qdrant is healthy, False otherwise
        """
        client = await cls.get_client_async()
        if not client:
            return False
        try:
            await client.get_collections()
            return True
        except Exception as e:
            logger.warning(f"Qdrant health check failed: {e}")
            return False

    @classmethod
    async def collection_exists(cls, collection_name: str) -> bool:
        """
        Check if a collection exists.

        Args:
            collection_name: Name of the collection to check

        Returns:
            True if collection exists, False otherwise
        """
        client = await cls.get_client_async()
        if not client:
            return False
        try:
            collections = await client.get_collections()
            return any(c.name == collection_name for c in collections.collections)
        except Exception as e:
            logger.warning(f"Failed to check collection existence: {e}")
            return False

    @classmethod
    async def create_collection(
        cls,
        collection_name: str,
        vector_size: int = 1536,
    ) -> bool:
        """
        Create a new collection if it doesn't exist.

        Args:
            collection_name: Name of the collection to create
            vector_size: Dimension of vectors (default: 1536 for OpenAI embeddings)

        Returns:
            True if collection exists or was created, False on failure
        """
        client = await cls.get_client_async()
        if not client:
            return False

        # Check if already exists
        if await cls.collection_exists(collection_name):
            logger.info(f"Collection '{collection_name}' already exists")
            return True

        try:
            await client.create_collection(
                collection_name=collection_name,
                vectors_config=qdrant_models.VectorParams(
                    size=vector_size,
                    distance=qdrant_models.Distance.COSINE,
                ),
            )
            logger.info(f"Created collection: {collection_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to create collection '{collection_name}': {e}")
            return False

    @classmethod
    async def upsert_points(
        cls,
        collection_name: str,
        points: List[Any],  # Use Any to avoid runtime evaluation when qdrant_models is None
    ) -> bool:
        """
        Upsert vectors to collection (non-blocking async).

        Args:
            collection_name: Target collection
            points: List of PointStruct objects to upsert

        Returns:
            True on success, False on failure
        """
        client = await cls.get_client_async()
        if not client:
            return False

        try:
            await client.upsert(
                collection_name=collection_name,
                points=points,
            )
            logger.info(f"Upserted {len(points)} points to '{collection_name}'")
            return True
        except Exception as e:
            logger.error(f"Failed to upsert points to '{collection_name}': {e}")
            return False

    @classmethod
    async def search(
        cls,
        collection_name: str,
        query_vector: List[float],
        limit: int = 5,
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for similar vectors.

        Args:
            collection_name: Collection to search in
            query_vector: Query embedding vector
            limit: Maximum number of results
            score_threshold: Minimum similarity score (optional)

        Returns:
            List of matches with {canonical_name, score, payload}
        """
        client = await cls.get_client_async()
        if not client:
            return []

        start_time = time.perf_counter()
        matches = []
        try:
            # Use query_points for qdrant-client >= 1.7.0
            results = await client.query_points(
                collection_name=collection_name,
                query=query_vector,
                limit=limit,
                score_threshold=score_threshold,
            )

            matches = [
                {
                    "canonical_name": hit.payload.get("canonical_name") if hit.payload else None,
                    "score": hit.score,
                    "payload": hit.payload or {},
                }
                for hit in results.points
            ]
            return matches
        except Exception as e:
            logger.error(f"Qdrant search failed in '{collection_name}': {e}")
            return []
        finally:
            # Fire-and-forget: Log to Langfuse
            latency_ms = (time.perf_counter() - start_time) * 1000
            top_score = matches[0]["score"] if matches else None
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(langfuse_service.log_vector_search(
                    collection_name=collection_name,
                    query_text="[vector]",
                    result_count=len(matches),
                    top_score=top_score,
                    latency_ms=latency_ms,
                    metadata={"limit": limit, "score_threshold": score_threshold}
                ))
            except RuntimeError:
                pass  # No event loop - skip logging (graceful degradation)

    @classmethod
    async def delete_collection(cls, collection_name: str) -> bool:
        """
        Delete a collection (useful for testing/cleanup).

        Args:
            collection_name: Name of the collection to delete

        Returns:
            True on success, False on failure
        """
        client = await cls.get_client_async()
        if not client:
            return False

        try:
            await client.delete_collection(collection_name=collection_name)
            logger.info(f"Deleted collection: {collection_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete collection '{collection_name}': {e}")
            return False

    @classmethod
    async def get_collection_info(cls, collection_name: str) -> Optional[Dict[str, Any]]:
        """
        Get collection information (vector count, config, etc.).

        Args:
            collection_name: Name of the collection

        Returns:
            Collection info dict or None on failure
        """
        client = await cls.get_client_async()
        if not client:
            return None

        try:
            info = await client.get_collection(collection_name=collection_name)
            return {
                "name": collection_name,
                "vectors_count": info.points_count,  # points_count is available in newer API
                "points_count": info.points_count,
                "status": info.status.value if info.status else None,
            }
        except Exception as e:
            logger.warning(f"Failed to get collection info for '{collection_name}': {e}")
            return None
