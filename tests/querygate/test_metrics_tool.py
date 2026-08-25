"""Metric compilation: the server writes the aggregation, not the model."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("duckdb", reason="install querygate[duckdb] to run these")

from querygate import config
from querygate.catalog.loaders import bundle
from querygate.query.metrics import build_metric_query, resolve_metric
from querygate.query.validation import GroundingError
from querygate.warehouse import duckdb_backend

_SEED = """
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE OR REPLACE TABLE analytics.customer_orders AS SELECT * FROM (VALUES
    (1,'acme',10,'Alice','NA',DATE '2024-01-05',100.0,'completed','Pro'),
    (2,'acme',11,'Bob','EU',DATE '2024-02-05',50.0,'completed','Free'),
    (3,'acme',12,'Cy','NA',DATE '2024-06-05',25.0,'pending','Pro'),
    (4,'globex',20,'Dee','NA',DATE '2024-01-09',300.0,'completed','Pro')
) AS t(order_id,tenant_id,customer_id,customer_name,region,order_date,amount,status,plan_name);
"""


@pytest.fixture(autouse=True)
def duckdb_engine(monkeypatch):
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


def _run(catalog, scopes={"acme"}, **kwargs):  # noqa: B006 (read-only default)
    from querygate import warehouse

    metric = resolve_metric(catalog, kwargs.pop("metric", "completed_revenue"))
    prepared = build_metric_query(catalog, metric, frozenset(scopes), **kwargs)
    return prepared, asyncio.run(warehouse.execute(prepared)).rows


class TestDefinitionIsApplied:
    def test_metric_filter_comes_from_the_definition(self, catalog):
        """The pending 25.0 order is excluded because the definition says
        completed-only, not because the agent remembered to say so."""
        _, rows = _run(catalog)
        assert float(rows[0]["completed_revenue"]) == 150.0

    def test_dimensions_group_and_order(self, catalog):
        _, rows = _run(catalog, dimensions=["region"])
        assert [(r["region"], float(r["completed_revenue"])) for r in rows] == [
            ("EU", 50.0),
            ("NA", 100.0),
        ]

    def test_result_reports_the_definition_used(self, catalog):
        """So a human can audit which rule produced the number."""
        metric = resolve_metric(catalog, "completed_revenue")
        assert metric.expr == "sum(amount)"
        assert metric.filter == "status = 'completed'"


class TestGovernanceStillApplies:
    def test_metric_is_tenant_scoped(self, catalog):
        _, acme = _run(catalog, scopes={"acme"})
        _, globex = _run(catalog, scopes={"globex"})
        assert float(acme[0]["completed_revenue"]) == 150.0
        assert float(globex[0]["completed_revenue"]) == 300.0

    def test_compiled_sql_carries_the_tenant_filter(self, catalog):
        prepared, _ = _run(catalog)
        assert "tenant_id = ANY" in prepared.sql

    def test_empty_scope_is_refused(self, catalog):
        from querygate.query.governance import GovernanceError

        with pytest.raises(GovernanceError):
            _run(catalog, scopes=set())


class TestTimeRange:
    def test_start_filters(self, catalog):
        _, rows = _run(catalog, time_column="order_date", start="2024-02-01")
        assert float(rows[0]["completed_revenue"]) == 50.0

    def test_end_filters(self, catalog):
        _, rows = _run(catalog, time_column="order_date", end="2024-01-31")
        assert float(rows[0]["completed_revenue"]) == 100.0

    def test_non_date_input_is_refused(self, catalog):
        """The parse is the sanitiser: anything surviving fromisoformat is a
        real date, which is why it can be embedded as a literal."""
        with pytest.raises(GroundingError, match="ISO date"):
            _run(catalog, time_column="order_date", start="2024-01-01'; DROP TABLE x--")

    def test_time_range_without_column_is_refused(self, catalog):
        with pytest.raises(GroundingError, match="time_column"):
            _run(catalog, start="2024-01-01")

    def test_non_date_column_is_refused(self, catalog):
        with pytest.raises(GroundingError, match="not a date"):
            _run(catalog, time_column="region", start="2024-01-01")


class TestValidation:
    def test_unknown_metric_lists_the_real_ones(self, catalog):
        with pytest.raises(GroundingError, match="completed_revenue"):
            resolve_metric(catalog, "made_up_metric")

    def test_unknown_dimension_is_refused(self, catalog):
        with pytest.raises(GroundingError, match="Dimension"):
            _run(catalog, dimensions=["nonexistent"])

    def test_masked_dimension_is_refused_with_a_clear_reason(self, catalog):
        """Grouping by a masked column is blocked upstream anyway; saying so
        here gives the agent a better error than a generic masking rejection."""
        with pytest.raises(GroundingError, match="masked"):
            _run(catalog, dimensions=["customer_name"])
