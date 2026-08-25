"""Rate limiter: bucket maths, per-caller isolation, and the Redis/Lua backend.

The Redis cases are skipped unless QG_TEST_REDIS_URL points at a real server.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from querygate.ratelimit.base import NoopLimiter, RateLimitError
from querygate.ratelimit.memory import MemoryTokenBucketLimiter


class TestNoop:
    async def test_always_allows(self):
        limiter = NoopLimiter()
        for _ in range(100):
            await limiter.acquire("anyone")


class TestMemoryBucket:
    async def test_burst_then_reject(self):
        limiter = MemoryTokenBucketLimiter(rate_per_minute=60, burst=3)
        for _ in range(3):
            await limiter.acquire("alice")
        with pytest.raises(RateLimitError):
            await limiter.acquire("alice")

    async def test_retry_after_is_reported(self):
        limiter = MemoryTokenBucketLimiter(rate_per_minute=60, burst=1)
        await limiter.acquire("alice")
        with pytest.raises(RateLimitError) as excinfo:
            await limiter.acquire("alice")
        # 60/min = 1 token/sec, so a fresh token is ~1s away.
        assert 0 < excinfo.value.retry_after <= 1.01

    async def test_callers_have_separate_buckets(self):
        """The point of per-caller keying: one noisy tenant must not starve another."""
        limiter = MemoryTokenBucketLimiter(rate_per_minute=60, burst=2)
        await limiter.acquire("acme")
        await limiter.acquire("acme")
        with pytest.raises(RateLimitError):
            await limiter.acquire("acme")
        # globex is untouched by acme burning its quota.
        await limiter.acquire("globex")
        await limiter.acquire("globex")

    async def test_cost_weighting(self):
        """An expensive tool drains the bucket faster than a cheap one."""
        limiter = MemoryTokenBucketLimiter(rate_per_minute=600, burst=10)
        await limiter.acquire("alice", cost=5)  # run_query
        await limiter.acquire("alice", cost=5)  # run_query
        with pytest.raises(RateLimitError):
            await limiter.acquire("alice", cost=5)

    async def test_refill_over_time(self):
        limiter = MemoryTokenBucketLimiter(rate_per_minute=6000, burst=1)  # 100 tokens/sec
        await limiter.acquire("alice")
        with pytest.raises(RateLimitError):
            await limiter.acquire("alice")
        await asyncio.sleep(0.05)  # ~5 tokens regenerate
        await limiter.acquire("alice")

    async def test_concurrent_callers_never_oversell(self):
        """20 concurrent calls against a 5-token bucket must allow exactly 5."""
        limiter = MemoryTokenBucketLimiter(rate_per_minute=0.001, burst=5)
        results = await asyncio.gather(
            *(limiter.check("alice") for _ in range(20)),
        )
        assert sum(1 for r in results if r.allowed) == 5

    async def test_idle_buckets_are_evicted(self):
        limiter = MemoryTokenBucketLimiter(rate_per_minute=60, burst=1, idle_ttl=0.0)
        for i in range(1100):
            await limiter.check(f"caller-{i}")
        # Eviction kicks in past the threshold; the dict must not grow unbounded.
        assert len(limiter._buckets) < 1100


_REDIS_URL = os.environ.get("QG_TEST_REDIS_URL")
redis_test = pytest.mark.skipif(not _REDIS_URL, reason="set QG_TEST_REDIS_URL to test the Lua backend")


@redis_test
class TestRedisLuaBucket:
    @pytest.fixture
    async def limiter(self):
        from redis.asyncio import Redis

        from querygate.ratelimit.redis_backend import RedisTokenBucketLimiter

        client = Redis.from_url(_REDIS_URL, decode_responses=True)
        # Unique prefix per test run so repeated runs never collide.
        yield RedisTokenBucketLimiter(
            client, rate_per_minute=60, burst=3, key_prefix=f"qgtest:{uuid.uuid4().hex[:8]}"
        )
        await client.aclose()

    async def test_burst_then_reject(self, limiter):
        for _ in range(3):
            await limiter.acquire("alice")
        with pytest.raises(RateLimitError):
            await limiter.acquire("alice")

    async def test_separate_buckets_per_caller(self, limiter):
        for _ in range(3):
            await limiter.acquire("acme")
        with pytest.raises(RateLimitError):
            await limiter.acquire("acme")
        await limiter.acquire("globex")

    async def test_cost_weighting(self, limiter):
        await limiter.acquire("alice", cost=3)
        with pytest.raises(RateLimitError):
            await limiter.acquire("alice", cost=1)

    async def test_atomic_under_concurrency(self, limiter):
        """The Lua script serializes the whole refill-and-spend cycle: 50 parallel
        calls against a 3-token bucket allow exactly 3, with no read-modify-write race."""
        results = await asyncio.gather(*(limiter.check("racer") for _ in range(50)))
        assert sum(1 for r in results if r.allowed) == 3

    async def test_refill_uses_redis_clock(self, limiter):
        for _ in range(3):
            await limiter.acquire("alice")
        with pytest.raises(RateLimitError):
            await limiter.acquire("alice")
        await asyncio.sleep(1.1)  # 60/min = 1 token/sec
        await limiter.acquire("alice")

    async def test_bucket_key_has_ttl(self, limiter):
        from redis.asyncio import Redis

        await limiter.acquire("ttl-check")
        client = Redis.from_url(_REDIS_URL, decode_responses=True)
        ttl = await client.ttl(f"{limiter._prefix}:ttl-check")
        await client.aclose()
        assert 0 < ttl <= 900


class TestFailOpen:
    """A limiter that cannot reach its store must not take the service down.
    Governance is the opposite, and always fails closed."""

    async def _broken_limiter(self, *, fail_open: bool):
        from querygate.ratelimit.redis_backend import RedisTokenBucketLimiter

        class BrokenClient:
            def register_script(self, _script):
                async def _raise(*_args, **_kwargs):
                    raise ConnectionError("redis is down")

                return _raise

        return RedisTokenBucketLimiter(BrokenClient(), rate_per_minute=60, burst=3, fail_open=fail_open)

    async def test_allows_when_fail_open(self):
        limiter = await self._broken_limiter(fail_open=True)
        await limiter.acquire("alice")

    async def test_rejects_when_fail_closed(self):
        limiter = await self._broken_limiter(fail_open=False)
        with pytest.raises(RateLimitError):
            await limiter.acquire("alice")


class TestFactory:
    def test_disabled_returns_noop(self):
        from querygate.ratelimit import create_limiter

        assert isinstance(create_limiter(rate_per_minute=0), NoopLimiter)

    def test_memory_when_no_redis_url(self):
        from querygate.ratelimit import create_limiter

        limiter = create_limiter(rate_per_minute=60, burst=5, redis_url="")
        assert isinstance(limiter, MemoryTokenBucketLimiter)


class TestBucketKeyScope:
    """Scope decides who shares a bucket. Outside a request there is no
    principal, so every scope must still produce a stable, non-empty key."""

    def test_global_scope(self, monkeypatch):
        from querygate import config
        from querygate.ratelimit import resolve_bucket_key

        monkeypatch.setattr(config, "RATE_LIMIT_SCOPE", "global")
        assert resolve_bucket_key("run_query") == "global"

    def test_anonymous_without_principal(self, monkeypatch):
        from querygate import config
        from querygate.ratelimit import resolve_bucket_key

        monkeypatch.setattr(config, "RATE_LIMIT_SCOPE", "principal")
        assert resolve_bucket_key("run_query") == "anonymous"
