"""
TTL-based cache for Service Config Agent.

Provides in-memory caching with automatic expiration for frequently-used data:
- Parameter resolution results
- Service lookups
- Config metadata

Thread-safe and async-compatible.
"""

import time
import asyncio
from typing import Any, Dict, Optional, TypeVar, Callable, Awaitable
from functools import wraps
import logging

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TTLCache:
    """
    Simple TTL-based in-memory cache.

    Features:
    - Automatic expiration
    - Thread-safe operations
    - Size limit with LRU eviction
    - Hit/miss metrics
    """

    def __init__(self, ttl_seconds: int = 300, max_size: int = 1000):
        """
        Initialize cache.

        Args:
            ttl_seconds: Time to live in seconds (default 5 minutes)
            max_size: Maximum cache entries before eviction
        """
        self._cache: Dict[str, tuple[Any, float]] = {}
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        async with self._lock:
            if key in self._cache:
                value, expiry = self._cache[key]
                if time.time() < expiry:
                    self._hits += 1
                    return value
                else:
                    # Expired - remove
                    del self._cache[key]
            self._misses += 1
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Set value in cache with expiry."""
        async with self._lock:
            # Evict oldest if at capacity
            if len(self._cache) >= self._max_size:
                self._evict_oldest()

            expiry = time.time() + (ttl or self._ttl)
            self._cache[key] = (value, expiry)

    async def delete(self, key: str) -> None:
        """Delete key from cache."""
        async with self._lock:
            self._cache.pop(key, None)

    async def clear(self) -> None:
        """Clear all cache entries."""
        async with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def _evict_oldest(self) -> None:
        """Evict oldest (earliest expiry) entry."""
        if not self._cache:
            return
        oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][1])
        del self._cache[oldest_key]

    @property
    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0
        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(hit_rate, 3),
        }


# Singleton caches for different data types
_parameter_cache = TTLCache(ttl_seconds=600, max_size=500)  # 10 min for params
_service_cache = TTLCache(ttl_seconds=300, max_size=200)    # 5 min for services


def cached_async(
    cache: TTLCache,
    key_fn: Callable[..., str],
    ttl: Optional[int] = None,
):
    """
    Decorator for caching async function results.

    Args:
        cache: TTLCache instance to use
        key_fn: Function to generate cache key from args
        ttl: Optional custom TTL for this cache

    Usage:
        @cached_async(_parameter_cache, lambda query: f"param:{query}")
        async def resolve_parameter(query: str) -> dict:
            ...
    """
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            key = key_fn(*args, **kwargs)

            # Check cache
            cached = await cache.get(key)
            if cached is not None:
                logger.debug(f"Cache hit: {key}")
                return cached

            # Call function and cache result
            result = await func(*args, **kwargs)
            await cache.set(key, result, ttl)
            return result

        return wrapper
    return decorator


def get_parameter_cache() -> TTLCache:
    """Get the singleton parameter cache."""
    return _parameter_cache


def get_service_cache() -> TTLCache:
    """Get the singleton service cache."""
    return _service_cache


async def clear_all_caches() -> None:
    """Clear all caches. Useful for testing or manual refresh."""
    await _parameter_cache.clear()
    await _service_cache.clear()
    logger.info("All caches cleared")


def get_cache_stats() -> Dict[str, Any]:
    """Get stats for all caches."""
    return {
        "parameter_cache": _parameter_cache.stats,
        "service_cache": _service_cache.stats,
    }
