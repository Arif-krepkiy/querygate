"""Redis token bucket, shared across replicas.

The arithmetic lives in scripts/token_bucket.lua so refill-and-spend is one
atomic operation.
"""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING

from querygate.log_setup import get_logger
from querygate.ratelimit.base import BaseLimiter, Decision

if TYPE_CHECKING:
    from redis.asyncio import Redis

_log = get_logger("querygate.ratelimit")

# Idle buckets expire so Redis never accumulates keys for callers who left.
_BUCKET_TTL_SECONDS = 900


def _load_script() -> str:
    return files("querygate.ratelimit.scripts").joinpath("token_bucket.lua").read_text(encoding="utf-8")


class RedisTokenBucketLimiter(BaseLimiter):
    """Distributed token bucket. Shared by every process pointing at this Redis."""

    def __init__(
        self,
        client: Redis,
        rate_per_minute: float,
        burst: int,
        *,
        fail_open: bool = True,
        key_prefix: str = "qg:rl",
    ) -> None:
        if rate_per_minute <= 0:
            msg = "rate_per_minute must be positive."
            raise ValueError(msg)
        self._client = client
        self._rate = rate_per_minute / 60.0
        self._capacity = float(max(1, burst))
        self._fail_open = fail_open
        self._prefix = key_prefix
        self._script = client.register_script(_load_script())
        _log.info(
            "Redis rate limiter: %.1f req/min, burst=%d, fail_%s",
            rate_per_minute,
            burst,
            "open" if fail_open else "closed",
        )

    async def check(self, key: str, cost: float = 1.0) -> Decision:
        try:
            # now=0 tells the script to use the Redis server clock, so skew
            # between app replicas cannot distort anyone's window.
            raw = await self._script(
                keys=[f"{self._prefix}:{key}"],
                args=[self._capacity, self._rate, 0, cost, _BUCKET_TTL_SECONDS],
            )
        except Exception as exc:
            # Rate limiting guards availability, not confidentiality. Taking the
            # whole service down because the limiter is unreachable trades a
            # small risk for a certain outage, so the default is to allow and
            # log loudly. Flip QG_RATE_LIMIT_FAIL_OPEN to invert that.
            if self._fail_open:
                _log.warning("rate limiter unavailable, allowing request: %s", exc)
                return Decision(allowed=True)
            _log.error("rate limiter unavailable, rejecting request: %s", exc)
            return Decision(allowed=False, retry_after=1.0)

        allowed, tokens_left, retry_after = raw
        return Decision(
            allowed=bool(int(allowed)),
            tokens_left=float(tokens_left),
            retry_after=float(retry_after),
        )

    async def close(self) -> None:
        await self._client.aclose()
