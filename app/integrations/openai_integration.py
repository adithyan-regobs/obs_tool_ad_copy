"""
OpenAI Integration for embeddings and chat.

Provides singleton wrapper for OpenAI API with lazy initialization,
following the same pattern as QdrantIntegration.
"""
import asyncio
import logging
import time
from typing import Optional, List

from openai import AsyncOpenAI
from langchain_openai import ChatOpenAI

from app.core.config import settings
from app.services.langfuse_service import langfuse_service

logger = logging.getLogger(__name__)


class OpenAIIntegration:
    """
    Singleton wrapper for OpenAI with lazy initialization.

    Provides:
    - Lazy client initialization with settings
    - Single text embedding generation
    - Batch embedding generation (more efficient)
    - Chat client for LLM operations
    - Graceful error handling (returns None on failures)
    """

    _client: Optional[AsyncOpenAI] = None
    _initialized: bool = False
    _chat_clients: dict = {}  # Cache chat clients by temperature

    @classmethod
    def _get_client(cls) -> Optional[AsyncOpenAI]:
        """
        Get or create AsyncOpenAI client instance.

        Returns:
            AsyncOpenAI instance or None if initialization failed
        """
        if not cls._initialized:
            cls._initialize_client()
        return cls._client

    @classmethod
    def _initialize_client(cls) -> None:
        """Initialize OpenAI client with settings from config."""
        try:
            cls._client = AsyncOpenAI(api_key=settings.openai_api_key)
            cls._initialized = True
            logger.info("OpenAI client initialized for embeddings")
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {e}")
            cls._client = None
            cls._initialized = True  # Mark as attempted to avoid repeated failures

    @classmethod
    def reset_client(cls) -> None:
        """Reset client for re-initialization (useful for testing)."""
        cls._client = None
        cls._initialized = False
        cls._chat_clients = {}

    @classmethod
    async def embed_text(cls, text: str) -> Optional[List[float]]:
        """
        Generate embedding for a single text.

        Args:
            text: Text to embed (will be stripped of whitespace)

        Returns:
            Embedding vector (1536 dimensions) or None on failure
        """
        if not text or not text.strip():
            logger.warning("Empty text provided for embedding")
            return None

        client = cls._get_client()
        if not client:
            return None

        start_time = time.perf_counter()
        success = False
        try:
            response = await client.embeddings.create(
                input=text.strip(),
                model=settings.embedding_model,
            )
            success = True
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"Embedding generation failed: {e}")
            return None
        finally:
            latency_ms = (time.perf_counter() - start_time) * 1000
            asyncio.create_task(langfuse_service.log_embedding_operation(
                operation_type="single",
                text_count=1,
                latency_ms=latency_ms,
                success=success,
                metadata={"text_length": len(text.strip())}
            ))

    @classmethod
    async def embed_batch(cls, texts: List[str]) -> List[Optional[List[float]]]:
        """
        Generate embeddings for multiple texts in a single API call.

        More efficient than calling embed_text() multiple times.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors (None for failed items),
            maintaining input order
        """
        if not texts:
            return []

        client = cls._get_client()
        if not client:
            return [None] * len(texts)

        # Filter empty texts but track original positions
        valid_texts = []
        valid_indices = []
        for i, text in enumerate(texts):
            if text and text.strip():
                valid_texts.append(text.strip())
                valid_indices.append(i)

        if not valid_texts:
            logger.warning("No valid texts provided for batch embedding")
            return [None] * len(texts)

        start_time = time.perf_counter()
        success = False
        try:
            response = await client.embeddings.create(
                input=valid_texts,
                model=settings.embedding_model,
            )

            # Initialize result list with None
            result: List[Optional[List[float]]] = [None] * len(texts)

            # Place embeddings at correct positions
            for item in response.data:
                original_index = valid_indices[item.index]
                result[original_index] = item.embedding

            success = True
            return result

        except Exception as e:
            logger.error(f"Batch embedding generation failed: {e}")
            return [None] * len(texts)
        finally:
            latency_ms = (time.perf_counter() - start_time) * 1000
            asyncio.create_task(langfuse_service.log_embedding_operation(
                operation_type="batch",
                text_count=len(valid_texts),
                latency_ms=latency_ms,
                success=success,
                metadata={"total_requested": len(texts), "valid_count": len(valid_texts)}
            ))

    @classmethod
    def get_embedding_dimension(cls) -> int:
        """
        Get the dimension of embeddings produced by the configured model.

        Returns:
            Embedding dimension (1536 for text-embedding-3-small)
        """
        return settings.embedding_dimensions

    @classmethod
    def get_chat_client(cls, temperature: Optional[float] = None, model: Optional[str] = None) -> ChatOpenAI:
        """
        Get ChatOpenAI client for LLM operations.

        Caches clients by model and temperature for reuse.

        Args:
            temperature: Override temperature (default: from settings)
            model: Override model (default: from settings)

        Returns:
            ChatOpenAI instance
        """
        temp = temperature if temperature is not None else settings.openai_temperature
        mdl = model if model is not None else settings.openai_model
        cache_key = f"model_{mdl}_temp_{temp}"

        if cache_key not in cls._chat_clients:
            cls._chat_clients[cache_key] = ChatOpenAI(
                api_key=settings.openai_api_key,
                model=mdl,
                temperature=temp,
            )
            logger.info(f"Chat client initialized (model={mdl}, temperature={temp})")

        return cls._chat_clients[cache_key]
