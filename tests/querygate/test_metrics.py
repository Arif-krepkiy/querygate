"""Metric ingestion, and that a definition matches its ground-truth number."""

from __future__ import annotations

import asyncio
import os

import pytest

from querygate import config
from querygate.catalog.loaders import bundle
from querygate.query.prepare import prepare_query


class TestMetricCatalog:
    def test_metrics_loaded(self, catalog):
        names = {m.name for m in catalog.metrics}
        assert {"completed_revenue", "total_orders", "active_customers"} <= names

    def test_metrics_for_model(self, catalog):
        metrics = catalog.metrics_for("customer_orders")
        assert "completed_revenue" in {m.name for m in metrics}

    def test_metric_has_definition(self, catalog):
        m = catalog.get_metric("completed_revenue")
        assert m is not None
        assert m.expr == "sum(amount)"
        assert m.filter == "status = 'completed'"


_DSN = os.environ.get("QG_TEST_PG_DSN")


@pytest.mark.skipif(not _DSN, reason="set QG_TEST_PG_DSN to run against Postgres")
def test_metric_definition_matches_ground_truth():
    """The completed_revenue metric, computed from its expr+filter, equals the
    golden number, so the definition the agent is handed is correct."""
    config.PG_DSN = _DSN  # type: ignore[assignment]
    from querygate import warehouse

    catalog = bundle.load_bundle(config.CATALOG_LOCAL_PATH)
    metric = catalog.get_metric("completed_revenue")
    sql = f"SELECT {metric.expr} AS v FROM {metric.model} WHERE {metric.filter}"
    prepared = prepare_query(sql, catalog, frozenset({"acme"}))
    rows = asyncio.run(warehouse.execute(prepared)).rows
    assert round(float(rows[0]["v"]), 2) == 55952.59
