"""Result caching. Off by default; in-process or Redis when enabled."""

from __future__ import annotations

from querygate import config
from querygate.cache.base import CacheBackend, CacheKey, NoopCache
from querygate.cache.memory import MemoryCache
from querygate.log_setup import get_logger

_log = get_logger("querygate.cache")

_cache: CacheBackend | None = None


def create_cache(ttl: int | None = None, redis_url: str | None = None) -> CacheBackend:
    """Build the configured backend (arguments override config, for tests)."""
    effective_ttl = config.CACHE_TTL_SECONDS if ttl is None else ttl
    if effective_ttl <= 0:
        return NoopCache()

    url = config.REDIS_URL if redis_url is None else redis_url
    if url:
        try:
            from redis.asyncio import Redis

            from querygate.cache.redis_backend import RedisCache

            _log.info("Redis result cache enabled (ttl=%ds)", effective_ttl)
            return RedisCache(Redis.from_url(url, decode_responses=True))
        except ImportError:
            _log.error(
                "QG_REDIS_URL is set but the redis extra is missing "
                "(pip install 'querygate[redis]'); using an in-process cache instead."
            )

    _log.info("In-process result cache enabled (ttl=%ds)", effective_ttl)
    return MemoryCache()


def get_cache() -> CacheBackend:
    """The process-wide cache, built on first use."""
    global _cache
    if _cache is None:
        _cache = create_cache()
    return _cache


def reset_cache(cache: CacheBackend | None = None) -> None:
    """Replace (or clear) the process-wide cache. For tests and shutdown."""
    global _cache
    _cache = cache


__all__ = [
    "CacheBackend",
    "CacheKey",
    "MemoryCache",
    "NoopCache",
    "create_cache",
    "get_cache",
    "reset_cache",
]
