from __future__ import annotations

from querygate import config
from querygate.log_setup import get_logger
from querygate.ratelimit.base import BaseLimiter, NoopLimiter
from querygate.ratelimit.memory import MemoryTokenBucketLimiter

_log = get_logger("querygate.ratelimit")

_limiter: BaseLimiter | None = None


def create_limiter(
    rate_per_minute: float | None = None,
    burst: int | None = None,
    redis_url: str | None = None,
    *,
    fail_open: bool | None = None,
) -> BaseLimiter:
    """Pick a backend from config (arguments override, for tests).

    * rate <= 0            → NoopLimiter (disabled)
    * redis_url set        → shared Redis token bucket (correct when scaled out)
    * otherwise            → in-process token bucket
    """
    rpm = config.RATE_LIMIT_RPM if rate_per_minute is None else rate_per_minute
    if rpm <= 0:
        _log.info("Rate limiting disabled (QG_RATE_LIMIT_RPM=0).")
        return NoopLimiter()

    burst_size = config.RATE_LIMIT_BURST if burst is None else burst
    url = config.REDIS_URL if redis_url is None else redis_url

    if url:
        try:
            from redis.asyncio import Redis

            from querygate.ratelimit.redis_backend import RedisTokenBucketLimiter

            client = Redis.from_url(url, decode_responses=True)
            return RedisTokenBucketLimiter(
                client,
                rate_per_minute=rpm,
                burst=burst_size,
                fail_open=config.RATE_LIMIT_FAIL_OPEN if fail_open is None else fail_open,
            )
        except ImportError:
            # Asking for Redis and silently running a per-replica limiter would
            # be a misleading half-limit, so say so loudly.
            _log.error(
                "QG_REDIS_URL is set but the redis extra is not installed "
                "(pip install 'querygate[redis]'); falling back to an in-process limiter, "
                "which does NOT share state across replicas."
            )

    _log.info("In-process rate limiter: %.1f req/min, burst=%d", rpm, burst_size)
    return MemoryTokenBucketLimiter(rate_per_minute=rpm, burst=burst_size)


def get_limiter() -> BaseLimiter:
    """The process-wide limiter, built on first use."""
    global _limiter
    if _limiter is None:
        _limiter = create_limiter()
    return _limiter


def reset_limiter(limiter: BaseLimiter | None = None) -> None:
    """Replace (or clear) the process-wide limiter. For tests and shutdown."""
    global _limiter
    _limiter = limiter
