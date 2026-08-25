"""Column masking, the predicate oracle, and the unmask role."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("duckdb", reason="install querygate[duckdb] to run these")

from querygate import config
from querygate.catalog.loaders import bundle
from querygate.query.masking import MaskingError
from querygate.query.prepare import prepare_query
from querygate.warehouse import duckdb_backend

_SEED = """
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE OR REPLACE TABLE analytics.customer_orders AS SELECT * FROM (VALUES
    (1,'acme',10,'Alice Smith','NA',DATE '2024-01-05',100.0,'completed','Pro'),
    (2,'acme',11,'Bob Jones','EU',DATE '2024-02-05',50.0,'completed','Free'),
    (3,'acme',10,'Alice Smith','NA',DATE '2024-03-05',75.0,'completed','Pro')
) AS t(order_id,tenant_id,customer_id,customer_name,region,order_date,amount,status,plan_name);
"""


@pytest.fixture(autouse=True)
def duckdb_engine(monkeypatch):
    monkeypatch.setattr(config, "WAREHOUSE", "duckdb")
    monkeypatch.setattr(config, "SQL_DIALECT", "duckdb")
    monkeypatch.setattr(config, "DUCKDB_PATH", ":memory:")
    monkeypatch.setattr(config, "MASKING_ENABLED", True)
    duckdb_backend.reset_connection()
    con = duckdb_backend._connect()
    for statement in filter(str.strip, _SEED.split(";")):
        con.execute(statement)
    yield
    duckdb_backend.reset_connection()


@pytest.fixture
def catalog():
    return bundle.load_bundle(config.CATALOG_LOCAL_PATH)


def _rows(sql: str, catalog, **kwargs):
    from querygate import warehouse

    prepared = prepare_query(sql, catalog, frozenset({"acme"}), **kwargs)
    return asyncio.run(warehouse.execute(prepared)).rows


class TestCatalogPolicy:
    def test_mask_is_declared_not_inferred(self, catalog):
        """Policies come from catalog metadata a human wrote. Inferring PII from
        column names would mask 'email_preference' and miss 'contact'."""
        model = catalog.get_model("customer_orders")
        assert model.masked_columns() == {"customer_name": "hash"}

    def test_unmarked_columns_are_readable(self, catalog):
        model = catalog.get_model("customer_orders")
        assert "region" not in model.masked_columns()


class TestProjectionMasking:
    def test_bare_column_is_masked_and_keeps_its_name(self, catalog):
        rows = _rows("SELECT customer_name FROM customer_orders", catalog)
        assert "customer_name" in rows[0]
        assert "Alice" not in str(rows)

    def test_explicit_alias_is_preserved(self, catalog):
        rows = _rows("SELECT customer_name AS who FROM customer_orders", catalog)
        assert "who" in rows[0]
        assert "Alice" not in str(rows)

    def test_select_star_does_not_leak(self, catalog):
        """The lazy query is the one that matters. A masked value must not arrive
        just because the caller asked for everything."""
        rows = _rows("SELECT * FROM customer_orders", catalog)
        assert "Alice Smith" not in str(rows)

    def test_unmasked_columns_come_through(self, catalog):
        rows = _rows("SELECT region FROM customer_orders", catalog)
        assert {r["region"] for r in rows} == {"NA", "EU"}

    def test_hash_is_stable_so_grouping_still_works(self, catalog):
        """Analytical value survives: Alice appears twice and must group as one."""
        rows = _rows("SELECT count(DISTINCT customer_name) AS n FROM customer_orders", catalog)
        assert int(rows[0]["n"]) == 2  # Alice + Bob, not 3 rows


class TestPredicateOracle:
    """Masking only the projection is theatre if values can still be probed."""

    def test_equality_filter_rejected(self, catalog):
        with pytest.raises(MaskingError, match="masked"):
            prepare_query(
                "SELECT count(*) FROM customer_orders WHERE customer_name = 'Alice Smith'",
                catalog,
                frozenset({"acme"}),
            )

    def test_like_filter_rejected(self, catalog):
        """LIKE is a faster oracle than equality: one character at a time."""
        with pytest.raises(MaskingError):
            prepare_query(
                "SELECT count(*) FROM customer_orders WHERE customer_name LIKE 'A%'",
                catalog,
                frozenset({"acme"}),
            )

    def test_group_by_rejected(self, catalog):
        with pytest.raises(MaskingError):
            prepare_query(
                "SELECT customer_name, count(*) FROM customer_orders GROUP BY customer_name",
                catalog,
                frozenset({"acme"}),
            )

    def test_order_by_rejected(self, catalog):
        """Sorting leaks collation order, which is an alphabet-sized oracle."""
        with pytest.raises(MaskingError):
            prepare_query(
                "SELECT region FROM customer_orders ORDER BY customer_name",
                catalog,
                frozenset({"acme"}),
            )

    def test_having_rejected(self, catalog):
        with pytest.raises(MaskingError):
            prepare_query(
                "SELECT region FROM customer_orders GROUP BY region "
                "HAVING max(customer_name) = 'Alice Smith'",
                catalog,
                frozenset({"acme"}),
            )

    def test_error_explains_the_reason(self, catalog):
        """The agent should learn why, so it rewrites instead of retrying."""
        with pytest.raises(MaskingError, match="guessed one"):
            prepare_query(
                "SELECT count(*) FROM customer_orders WHERE customer_name = 'x'",
                catalog,
                frozenset({"acme"}),
            )


class TestUnmaskRole:
    def test_role_holder_reads_in_the_clear(self, catalog):
        rows = _rows("SELECT customer_name FROM customer_orders", catalog, unmask=True)
        assert "Alice Smith" in str(rows)

    def test_unmask_still_enforces_row_security(self, catalog):
        """Seeing a column is not seeing another tenant's rows. The two controls
        are independent, and lifting one must not lift the other."""
        prepared = prepare_query(
            "SELECT customer_name FROM customer_orders", catalog, frozenset({"acme"}), unmask=True
        )
        assert "tenant_id = ANY" in prepared.sql

    def test_masking_can_be_disabled_globally(self, catalog, monkeypatch):
        monkeypatch.setattr(config, "MASKING_ENABLED", False)
        rows = _rows("SELECT customer_name FROM customer_orders", catalog)
        assert "Alice Smith" in str(rows)
