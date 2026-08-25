"""DuckDB adapter, end to end. Needs no container and no credentials."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("duckdb", reason="install querygate[duckdb] to run these")

from querygate import config
from querygate.catalog.loaders import bundle
from querygate.query.governance import GovernanceError
from querygate.query.prepare import prepare_query
from querygate.query.validation import GroundingError
from querygate.warehouse import duckdb_backend

_SEED = """
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE OR REPLACE TABLE analytics.customer_orders AS SELECT * FROM (VALUES
    (1,'acme',10,'Alice','NA',DATE '2024-01-05',100.0,'completed','Pro'),
    (2,'acme',11,'Bob','EU',DATE '2024-02-05',50.0,'completed','Free'),
    (3,'globex',20,'Carl','NA',DATE '2024-01-09',300.0,'completed','Pro'),
    (4,'globex',21,'Dana','APAC',DATE '2024-03-01',70.0,'pending','Pro')
) AS t(order_id,tenant_id,customer_id,customer_name,region,order_date,amount,status,plan_name);
CREATE OR REPLACE TABLE analytics.plan_catalog AS SELECT * FROM (VALUES
    (1,'Free',0.0,'t0'),(2,'Pro',49.0,'t1'),(3,'Enterprise',299.0,'t2')
) AS t(plan_id,plan_name,monthly_price,tier);
"""


@pytest.fixture(autouse=True)
def duckdb_engine(monkeypatch):
    """Point the whole stack at an in-memory DuckDB for the duration of a test."""
    monkeypatch.setattr(config, "WAREHOUSE", "duckdb")
    monkeypatch.setattr(config, "SQL_DIALECT", "duckdb")
    monkeypatch.setattr(config, "DUCKDB_PATH", ":memory:")
    duckdb_backend.reset_connection()
    con = duckdb_backend._connect()
    for statement in filter(str.strip, _SEED.split(";")):
        con.execute(statement)
    yield
    duckdb_backend.reset_connection()


@pytest.fixture
def catalog():
    return bundle.load_bundle(config.CATALOG_LOCAL_PATH)


def _run(sql: str, scopes: set[str], catalog):
    from querygate import warehouse

    prepared = prepare_query(sql, catalog, frozenset(scopes))
    return asyncio.run(warehouse.execute(prepared)).rows


class TestTenantIsolation:
    def test_each_tenant_sees_only_its_own_rows(self, catalog):
        q = "SELECT sum(amount) AS total FROM customer_orders WHERE status = 'completed'"
        acme = float(_run(q, {"acme"}, catalog)[0]["total"])
        globex = float(_run(q, {"globex"}, catalog)[0]["total"])
        both = float(_run(q, {"acme", "globex"}, catalog)[0]["total"])
        assert (acme, globex) == (150.0, 300.0)
        assert acme + globex == both

    def test_governed_query_without_scope_is_refused(self, catalog):
        with pytest.raises(GovernanceError):
            _run("SELECT sum(amount) FROM customer_orders", set(), catalog)

    def test_public_table_needs_no_scope(self, catalog):
        rows = _run("SELECT plan_name FROM plan_catalog", set(), catalog)
        assert {r["plan_name"] for r in rows} == {"Free", "Pro", "Enterprise"}

    def test_unused_tenant_param_is_not_bound(self, catalog):
        """A public query carries no tenant predicate, so the parameter must not
        be bound. DuckDB rejects excess named parameters; Postgres tolerates them,
        which is how this stayed hidden until the second adapter landed."""
        prepared = prepare_query("SELECT plan_name FROM plan_catalog", catalog, frozenset())
        assert prepared.bind_params() == {}

    def test_join_of_governed_and_public(self, catalog):
        """Only the governed side is filtered; the public side joins freely.

        Ordered by order_id rather than customer_name because that column is
        masked, and sorting by a masked column is refused. See test_masking.
        """
        rows = _run(
            "SELECT o.order_id, p.monthly_price FROM customer_orders o "
            "JOIN plan_catalog p ON o.plan_name = p.plan_name ORDER BY o.order_id",
            {"acme"},
            catalog,
        )
        assert [r["order_id"] for r in rows] == [1, 2]  # acme's rows only
        assert [float(r["monthly_price"]) for r in rows] == [49.0, 0.0]  # Pro, Free


class TestPipelineParity:
    """The engine changes; the guarantees do not."""

    def test_dml_rejected(self, catalog):
        with pytest.raises(GroundingError):
            _run("DELETE FROM customer_orders", {"acme"}, catalog)

    def test_unknown_table_rejected(self, catalog):
        with pytest.raises(GroundingError):
            _run("SELECT * FROM ghosts", {"acme"}, catalog)

    def test_row_limit_applied(self, catalog):
        prepared = prepare_query("SELECT * FROM customer_orders", catalog, frozenset({"acme"}), limit=1)
        assert "LIMIT 1" in prepared.sql
        from querygate import warehouse

        assert len(asyncio.run(warehouse.execute(prepared)).rows) == 1

    def test_duckdb_placeholder_syntax(self, catalog):
        """sqlglot renders our :name placeholder to DuckDB's $name form."""
        prepared = prepare_query("SELECT sum(amount) FROM customer_orders", catalog, frozenset({"acme"}))
        assert "$qg_tenant_scopes" in prepared.sql
        assert "acme" not in prepared.sql  # bound, never interpolated


class TestCostEstimate:
    def test_estimate_returns_positive_row_estimate(self, catalog):
        from querygate import warehouse

        prepared = prepare_query("SELECT * FROM customer_orders", catalog, frozenset({"acme"}))
        assert asyncio.run(warehouse.estimate(prepared)).plan_cost > 0


class TestAdapterDispatch:
    def test_unknown_warehouse_is_a_clear_error(self, monkeypatch, catalog):
        from querygate import warehouse
        from querygate.warehouse.types import WarehouseError

        monkeypatch.setattr(config, "WAREHOUSE", "oracle")
        prepared = prepare_query("SELECT plan_name FROM plan_catalog", catalog, frozenset())
        with pytest.raises(WarehouseError, match="Supported"):
            asyncio.run(warehouse.execute(prepared))
