"""Result cache: TTL, eviction, and tenant isolation of the key."""

from __future__ import annotations

import asyncio

import pytest

from querygate.cache import CacheKey, MemoryCache, NoopCache, create_cache


class TestTenantIsolation:
    def test_same_query_different_tenants_differ(self):
        """The property the whole design rests on."""
        acme = CacheKey("filter_values", frozenset({"acme"}), ("customer_orders", "status", "", "100"))
        globex = CacheKey("filter_values", frozenset({"globex"}), ("customer_orders", "status", "", "100"))
        assert acme.digest() != globex.digest()

    def test_same_tenant_same_query_matches(self):
        parts = ("customer_orders", "status", "", "100")
        a = CacheKey("filter_values", frozenset({"acme"}), parts)
        b = CacheKey("filter_values", frozenset({"acme"}), parts)
        assert a.digest() == b.digest()

    def test_multi_tenant_scope_is_order_independent(self):
        parts = ("customer_orders", "status", "", "100")
        a = CacheKey("filter_values", frozenset({"acme", "globex"}), parts)
        b = CacheKey("filter_values", frozenset({"globex", "acme"}), parts)
        assert a.digest() == b.digest()

    def test_subset_scope_does_not_match_superset(self):
        """A single-tenant caller must not read a multi-tenant caller's entry."""
        parts = ("customer_orders", "status", "", "100")
        one = CacheKey("filter_values", frozenset({"acme"}), parts)
        both = CacheKey("filter_values", frozenset({"acme", "globex"}), parts)
        assert one.digest() != both.digest()

    def test_empty_scope_is_distinct(self):
        parts = ("plan_catalog", "plan_name", "", "100")
        anon = CacheKey("filter_values", frozenset(), parts)
        acme = CacheKey("filter_values", frozenset({"acme"}), parts)
        assert anon.digest() != acme.digest()

    def test_tenant_cannot_be_spoofed_through_parts(self):
        """Tenant scopes are hashed with a separator, so a crafted column name
        cannot make one caller's key collide with another's."""
        a = CacheKey("filter_values", frozenset({"acme"}), ("t", "col|globex"))
        b = CacheKey("filter_values", frozenset({"acme", "globex"}), ("t", "col"))
        assert a.digest() != b.digest()

    def test_key_requires_tenant_scopes(self):
        """There is no way to build a key without answering 'on whose behalf?'."""
        with pytest.raises(TypeError):
            CacheKey("filter_values", parts=("customer_orders", "status"))  # type: ignore[call-arg]


class TestMemoryCache:
    async def test_roundtrip(self):
        cache = MemoryCache()
        await cache.set("k", ["a", "b"], ttl=60)
        assert await cache.get("k") == ["a", "b"]

    async def test_miss_returns_none(self):
        assert await MemoryCache().get("nope") is None

    async def test_expiry(self):
        cache = MemoryCache()
        await cache.set("k", "v", ttl=1)
        assert await cache.get("k") == "v"
        # Expiry is checked on read against a monotonic deadline.
        cache._entries["k"] = ("v", 0.0)
        assert await cache.get("k") is None

    async def test_zero_ttl_does_not_store(self):
        cache = MemoryCache()
        await cache.set("k", "v", ttl=0)
        assert await cache.get("k") is None

    async def test_eviction_is_bounded(self):
        cache = MemoryCache(max_entries=10)
        for i in range(50):
            await cache.set(f"k{i}", i, ttl=60)
        assert len(cache._entries) == 10
        assert await cache.get("k0") is None  # oldest evicted
        assert await cache.get("k49") == 49


class TestNoopCache:
    async def test_never_stores(self):
        cache = NoopCache()
        await cache.set("k", "v", ttl=999)
        assert await cache.get("k") is None


class TestFactory:
    def test_disabled_by_default_ttl(self):
        assert isinstance(create_cache(ttl=0), NoopCache)

    def test_memory_when_no_redis(self):
        assert isinstance(create_cache(ttl=60, redis_url=""), MemoryCache)


class TestConcurrentUse:
    async def test_parallel_reads_and_writes(self):
        cache = MemoryCache()

        async def worker(i: int) -> None:
            await cache.set(f"k{i}", i, ttl=60)
            assert await cache.get(f"k{i}") == i

        await asyncio.gather(*(worker(i) for i in range(50)))
