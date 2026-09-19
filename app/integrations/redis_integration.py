"""
Redis Cache Integration.

Provides async client wrapper for Redis operations with lazy initialization,
connection pool management, and graceful error handling.

Used for distributed state management across multiple uvicorn workers, supporting:
- Conversation state caching
- Distributed locks for concurrency control
- Message tracking for UI invalidation
- Preview state persistence
"""
import asyncio
import logging
from typing import Optional, Any
import json

from redis import asyncio as aioredis
from redis.exceptions import RedisError, ConnectionError as RedisConnectionError

from app.core.config import settings

logger = logging.getLogger(__name__)


class RedisIntegration:
    """
    Singleton wrapper for Redis async client with lazy initialization.

    Provides:
    - Async client initialization with connection pooling
    - Health check for connectivity verification
    - Helper methods for common operations (get, set, delete, exists, ttl)
    - Distributed lock support with SET NX EX
    - Graceful error handling (returns None on failures)
    """

    _client: Optional[aioredis.Redis] = None
    _initialized: bool = False

    @classmethod
    async def get_client_async(cls) -> Optional[aioredis.Redis]:
        """
        Get or create async Redis client instance.

        Returns:
            Redis client instance or None if initialization failed or Redis is disabled
        """
        if not settings.redis_enabled:
            return None

        if not cls._initialized:
            await cls._initialize_client()
        return cls._client

    @classmethod
    async def _initialize_client(cls) -> None:
        """Initialize Redis async client with connection pool from config settings."""
        try:
            # Build connection URL
            password_part = f":{settings.redis_password}@" if settings.redis_password else ""
            redis_url = f"redis://{password_part}{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
            logger.info(f"~~~~~{redis_url}~~~~ redis url")

            # Create connection pool with timeouts
            cls._client = await aioredis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=settings.redis_timeout,
                socket_timeout=settings.redis_timeout,
            )

            # Test connection
            await cls._client.ping()

            cls._initialized = True
            connection_info = f"{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
            logger.info(f"Redis client initialized: {connection_info}")
        except Exception as e:
            logger.error(f"Failed to initialize Redis client: {e}")
            cls._client = None
            cls._initialized = True  # Mark as attempted to avoid repeated failures

    @classmethod
    def reset_client(cls) -> None:
        """Reset client for re-initialization (useful for testing)."""
        cls._client = None
        cls._initialized = False

    @classmethod
    async def close(cls) -> None:
        """Close Redis connection pool gracefully."""
        if cls._client:
            try:
                await cls._client.close()
                logger.info("Redis connection pool closed")
            except Exception as e:
                logger.warning(f"Error closing Redis connection: {e}")
        cls._client = None
        cls._initialized = False

    @classmethod
    async def health_check(cls) -> bool:
        """
        Check if Redis is reachable.

        Returns:
            True if Redis is healthy and responsive, False otherwise
        """
        client = await cls.get_client_async()
        if not client:
            return False
        try:
            response = await client.ping()
            return response is True
        except Exception as e:
            logger.warning(f"Redis health check failed: {e}")
            return False

    # Helper methods for common operations

    @classmethod
    async def get(cls, key: str) -> Optional[str]:
        """
        Get value from Redis by key.

        Args:
            key: Redis key

        Returns:
            Value as string or None if key doesn't exist or error occurred
        """
        client = await cls.get_client_async()
        if not client:
            return None
        try:
            return await client.get(key)
        except RedisError as e:
            logger.error(f"Redis GET failed for key '{key}': {e}")
            return None

    @classmethod
    async def get_json(cls, key: str) -> Optional[Any]:
        """
        Get JSON value from Redis by key.

        Args:
            key: Redis key

        Returns:
            Deserialized JSON object or None if key doesn't exist or error occurred
        """
        value = await cls.get(key)
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON for key '{key}': {e}")
            return None

    @classmethod
    async def set(cls, key: str, value: str, ttl: Optional[int] = None) -> bool:
        """
        Set value in Redis.

        Args:
            key: Redis key
            value: Value to store (string)
            ttl: Optional time-to-live in seconds

        Returns:
            True if successful, False otherwise
        """
        client = await cls.get_client_async()
        if not client:
            return False
        try:
            if ttl:
                await client.setex(key, ttl, value)
            else:
                await client.set(key, value)
            return True
        except RedisError as e:
            logger.error(f"Redis SET failed for key '{key}': {e}")
            return False

    @classmethod
    async def set_json(cls, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        Set JSON value in Redis.

        Args:
            key: Redis key
            value: Python object to serialize as JSON
            ttl: Optional time-to-live in seconds

        Returns:
            True if successful, False otherwise
        """
        try:
            json_str = json.dumps(value)
            return await cls.set(key, json_str, ttl)
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize JSON for key '{key}': {e}")
            return False

    @classmethod
    async def delete(cls, key: str) -> bool:
        """
        Delete key from Redis.

        Args:
            key: Redis key to delete

        Returns:
            True if key was deleted, False otherwise
        """
        client = await cls.get_client_async()
        if not client:
            return False
        try:
            result = await client.delete(key)
            return result > 0
        except RedisError as e:
            logger.error(f"Redis DELETE failed for key '{key}': {e}")
            return False

    @classmethod
    async def exists(cls, key: str) -> bool:
        """
        Check if key exists in Redis.

        Args:
            key: Redis key to check

        Returns:
            True if key exists, False otherwise
        """
        client = await cls.get_client_async()
        if not client:
            return False
        try:
            result = await client.exists(key)
            return result > 0
        except RedisError as e:
            logger.error(f"Redis EXISTS failed for key '{key}': {e}")
            return False

    @classmethod
    async def expire(cls, key: str, ttl: int) -> bool:
        """
        Set TTL on existing key.

        Args:
            key: Redis key
            ttl: Time-to-live in seconds

        Returns:
            True if TTL was set, False otherwise
        """
        client = await cls.get_client_async()
        if not client:
            return False
        try:
            result = await client.expire(key, ttl)
            return result
        except RedisError as e:
            logger.error(f"Redis EXPIRE failed for key '{key}': {e}")
            return False

    @classmethod
    async def ttl(cls, key: str) -> int:
        """
        Get remaining TTL for key.

        Args:
            key: Redis key

        Returns:
            TTL in seconds, -1 if no TTL, -2 if key doesn't exist, -3 on error
        """
        client = await cls.get_client_async()
        if not client:
            return -3
        try:
            return await client.ttl(key)
        except RedisError as e:
            logger.error(f"Redis TTL failed for key '{key}': {e}")
            return -3

    @classmethod
    async def acquire_lock(cls, lock_key: str, ttl: int = 10) -> bool:
        """
        Acquire distributed lock using SET NX EX.

        Args:
            lock_key: Redis key for the lock
            ttl: Lock expiration time in seconds (default: 10s to prevent stuck locks)

        Returns:
            True if lock was acquired, False if already locked or error occurred
        """
        client = await cls.get_client_async()
        if not client:
            return False
        try:
            # SET NX EX: Set if Not eXists with EXpiration
            result = await client.set(lock_key, "locked", ex=ttl, nx=True)
            return result is True
        except RedisError as e:
            logger.error(f"Redis acquire_lock failed for key '{lock_key}': {e}")
            return False

    @classmethod
    async def release_lock(cls, lock_key: str) -> bool:
        """
        Release distributed lock.

        Args:
            lock_key: Redis key for the lock

        Returns:
            True if lock was released, False otherwise
        """
        return await cls.delete(lock_key)
