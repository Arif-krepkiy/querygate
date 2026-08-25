"""In-process token bucket, used when no Redis is configured.

Correct for a single replica only: N processes each grant the full rate.
"""

from __future__ import annotations

import asyncio
import time

from querygate.ratelimit.base import BaseLimiter, Decision


class MemoryTokenBucketLimiter(BaseLimiter):
    """Per-key token bucket guarded by an asyncio lock."""

    def __init__(self, rate_per_minute: float, burst: int, *, idle_ttl: float = 900.0) -> None:
        if rate_per_minute <= 0:
            msg = "rate_per_minute must be positive."
            raise ValueError(msg)
        if burst < 1:
            msg = "burst must be at least 1."
            raise ValueError(msg)
        self._rate = rate_per_minute / 60.0
        self._capacity = float(burst)
        self._idle_ttl = idle_ttl
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, ts)
        self._lock = asyncio.Lock()

    def _evict_idle(self, now: float) -> None:
        """Drop buckets untouched for longer than the idle TTL.

        Without this, a server facing many distinct callers would accumulate a
        bucket per caller forever; an unbounded dict is a slow memory leak.
        """
        stale = [k for k, (_, ts) in self._buckets.items() if now - ts > self._idle_ttl]
        for key in stale:
            del self._buckets[key]

    async def check(self, key: str, cost: float = 1.0) -> Decision:
        async with self._lock:
            now = time.monotonic()
            if len(self._buckets) > 1000:
                self._evict_idle(now)

            tokens, ts = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + max(0.0, now - ts) * self._rate)

            if tokens < cost:
                self._buckets[key] = (tokens, now)
                return Decision(allowed=False, tokens_left=tokens, retry_after=(cost - tokens) / self._rate)

            tokens -= cost
            self._buckets[key] = (tokens, now)
            return Decision(allowed=True, tokens_left=tokens)
