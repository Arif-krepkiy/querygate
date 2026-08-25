from __future__ import annotations

import asyncio
import os

import pytest

from querygate import config
from querygate.catalog.loaders import bundle
from querygate.query.prepare import prepare_query

_DSN = os.environ.get("QG_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="set QG_TEST_PG_DSN to run warehouse integration tests")


@pytest.fixture(scope="module", autouse=True)
def _point_at_test_db():
    config.PG_DSN = _DSN  # type: ignore[assignment]


@pytest.fixture(scope="module")
def catalog():
    return bundle.load_bundle(config.CATALOG_LOCAL_PATH)


def _run(sql, scopes, catalog):
    from querygate import warehouse

    prepared = prepare_query(sql, catalog, frozenset(scopes))
    return asyncio.run(warehouse.execute(prepared)).rows


def test_tenant_isolation_sums_are_disjoint(catalog):
    q = "SELECT sum(amount) AS total FROM customer_orders WHERE status = 'completed'"
    acme = float(_run(q, {"acme"}, catalog)[0]["total"])
    globex = float(_run(q, {"globex"}, catalog)[0]["total"])
    both = float(_run(q, {"acme", "globex"}, catalog)[0]["total"])
    assert round(acme + globex, 2) == round(both, 2)
    assert acme != globex


def test_public_table_readable_without_scope(catalog):
    rows = _run("SELECT plan_name FROM plan_catalog", set(), catalog)
    assert {r["plan_name"] for r in rows} == {"Free", "Pro", "Enterprise"}


def test_cache_never_serves_across_tenants(catalog):
    """End-to-end: caching a tenant's result must not make it visible to another.

    Uses `region`, whose values happen to be identical for both tenants. The
    cache must still miss, because it cannot assume what the second caller is
    entitled to see.
    """
    from querygate.cache import CacheKey, MemoryCache
    from querygate.query.prepare import prepare_filter_values_query

    cache = MemoryCache()

    async def filter_values(scopes: set[str]) -> tuple[list, bool]:
        prepared = prepare_filter_values_query("customer_orders", "region", catalog, frozenset(scopes))
        key = CacheKey("filter_values", frozenset(scopes), ("customer_orders", "region")).digest()
        hit = await cache.get(key)
        if hit is not None:
            return hit, True
        from querygate import warehouse

        rows = (await warehouse.execute(prepared)).rows
        values = [r["value"] for r in rows]
        await cache.set(key, values, ttl=60)
        return values, False

    async def scenario() -> None:
        _, first = await filter_values({"acme"})
        _, second = await filter_values({"acme"})
        _, other_tenant = await filter_values({"globex"})
        assert first is False, "first call should populate the cache"
        assert second is True, "same tenant should hit the cache"
        assert other_tenant is False, "a different tenant must never hit another's entry"

    asyncio.run(scenario())
