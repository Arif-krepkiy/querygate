"""Join graph, data profiling and pagination."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("duckdb", reason="install querygate[duckdb] to run these")

from querygate import config
from querygate.catalog.loaders import bundle
from querygate.catalog.models import Join
from querygate.query import cursor as cursor_codec
from querygate.query.prepare import prepare_query
from querygate.query.profiling import build_profile_query, select_columns, shape_profile
from querygate.query.validation import GroundingError
from querygate.retrieval.slices import model_detail
from querygate.warehouse import duckdb_backend

_SEED = """
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE OR REPLACE TABLE analytics.customer_orders AS SELECT * FROM (VALUES
    (1,'acme',10,'Alice','NA',DATE '2024-01-05',100.0,'completed','Pro'),
    (2,'acme',11,'Bob','EU',DATE '2024-02-05',50.0,'completed','Free'),
    (3,'acme',12,NULL,'NA',DATE '2024-03-05',75.0,'pending','Pro'),
    (4,'acme',13,'Dana','APAC',DATE '2024-04-05',25.0,'completed','Pro'),
    (5,'globex',20,'Carl','NA',DATE '2024-01-09',300.0,'completed','Pro')
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


class TestJoinGraph:
    def test_joins_are_loaded(self, catalog):
        model = catalog.get_model("customer_orders")
        assert Join("plan_catalog", "plan_name", "plan_name") in model.joins

    def test_describe_model_exposes_joins(self, catalog):
        detail = model_detail(catalog.get_model("customer_orders"))
        targets = {j["to_model"] for j in detail["joins"]}
        assert "plan_catalog" in targets

    def test_model_without_joins_omits_the_key(self, catalog):
        """Absent rather than empty; an empty list is wasted context."""
        assert "joins" not in model_detail(catalog.get_model("plan_catalog"))

    def test_dbt_relationships_tests_become_joins(self):
        from querygate.catalog.loaders.dbt import _parse_relationship_tests

        manifest = {
            "nodes": {
                "test.p.rel": {
                    "test_metadata": {
                        "name": "relationships",
                        "kwargs": {
                            "column_name": "plan_name",
                            "field": "plan_name",
                            "to": "ref('plan_catalog')",
                            "model": "ref('customer_orders')",
                        },
                    }
                },
                "test.p.not_null": {"test_metadata": {"name": "not_null", "kwargs": {}}},
            }
        }
        joins = _parse_relationship_tests(manifest)
        assert joins["customer_orders"] == [Join("plan_catalog", "plan_name", "plan_name")]

    def test_malformed_test_is_skipped_not_fatal(self):
        from querygate.catalog.loaders.dbt import _parse_relationship_tests

        manifest = {"nodes": {"t": {"test_metadata": {"name": "relationships", "kwargs": {"to": "junk"}}}}}
        assert _parse_relationship_tests(manifest) == {}


class TestPartitionColumn:
    """dbt spells `partition_by` differently per adapter; a wrong read here
    would refuse valid queries, so anything unrecognised yields None."""

    def _read(self, partition_by):
        from querygate.catalog.loaders.dbt import _partition_column

        return _partition_column({"config": {"partition_by": partition_by}})

    def test_bigquery_dict_shape(self):
        assert self._read({"field": "order_date", "data_type": "date", "granularity": "day"}) == "order_date"

    def test_bare_string_shape(self):
        assert self._read("order_date") == "order_date"

    def test_absent(self):
        from querygate.catalog.loaders.dbt import _partition_column

        assert _partition_column({"config": {}}) is None
        assert _partition_column({}) is None

    def test_unrecognised_shape_yields_none(self):
        assert self._read(["order_date", "region"]) is None
        assert self._read({"data_type": "date"}) is None
        assert self._read("   ") is None

    def test_describe_model_exposes_it(self, catalog):
        assert model_detail(catalog.get_model("customer_orders"))["partition_column"] == "order_date"

    def test_unpartitioned_model_omits_the_key(self, catalog):
        assert "partition_column" not in model_detail(catalog.get_model("plan_catalog"))


class TestProfiling:
    def _profile(self, catalog, columns, scopes={"acme"}):  # noqa: B006 (read-only default)
        model = catalog.get_model("customer_orders")
        chosen = select_columns(model, columns)
        prepared = build_profile_query(model, chosen, catalog, frozenset(scopes))
        from querygate import warehouse

        rows = asyncio.run(warehouse.execute(prepared)).rows
        return shape_profile(rows[0], chosen, model)

    def test_counts_nulls_and_cardinality(self, catalog):
        profile = self._profile(catalog, ["customer_name", "region"])
        by_column = {c["column"]: c for c in profile["columns"]}
        assert profile["row_count"] == 4
        assert by_column["customer_name"]["non_null"] == 3
        assert by_column["customer_name"]["null_fraction"] == 0.25
        assert by_column["region"]["distinct"] == 3

    def test_range_only_for_ordered_types(self, catalog):
        profile = self._profile(catalog, ["amount", "region"])
        by_column = {c["column"]: c for c in profile["columns"]}
        assert "min" in by_column["amount"]
        assert "min" not in by_column["region"]  # min/max on text is noise

    def test_profile_is_tenant_scoped(self, catalog):
        """The whole point: a profile must describe only rows the caller may see.
        globex's 300.0 order must not widen acme's maximum."""
        acme = self._profile(catalog, ["amount"], scopes={"acme"})
        assert acme["row_count"] == 4
        assert float(acme["columns"][0]["max"]) == 100.0

        globex = self._profile(catalog, ["amount"], scopes={"globex"})
        assert globex["row_count"] == 1
        assert float(globex["columns"][0]["max"]) == 300.0

    def test_unknown_column_rejected(self, catalog):
        with pytest.raises(GroundingError, match="not in"):
            select_columns(catalog.get_model("customer_orders"), ["nonexistent"])

    def test_too_many_columns_rejected(self, catalog):
        with pytest.raises(GroundingError, match="at most"):
            select_columns(catalog.get_model("customer_orders"), ["region"] * 20)

    def test_default_selection_skips_tenant_column(self, catalog):
        """Profiling the tenant column is meaningless once the query is scoped
        to that very tenant, and would always report a single distinct value."""
        chosen = select_columns(catalog.get_model("customer_orders"), None)
        assert "tenant_id" not in chosen


class TestPagination:
    def test_offset_appears_in_sql(self, catalog):
        prepared = prepare_query(
            "SELECT region FROM customer_orders ORDER BY region",
            catalog,
            frozenset({"acme"}),
            limit=2,
            offset=2,
        )
        assert "OFFSET 2" in prepared.sql
        assert prepared.offset == 2

    def test_pages_do_not_overlap(self, catalog):
        from querygate import warehouse

        def page(offset: int) -> list[int]:
            prepared = prepare_query(
                "SELECT order_id FROM customer_orders ORDER BY order_id",
                catalog,
                frozenset({"acme"}),
                limit=2,
                offset=offset,
            )
            return [r["order_id"] for r in asyncio.run(warehouse.execute(prepared)).rows]

        assert page(0) == [1, 2]
        assert page(2) == [3, 4]

    def test_cursor_roundtrip(self):
        sql = "SELECT 1 FROM t"
        assert cursor_codec.decode(cursor_codec.encode(sql, 40), sql) == 40

    def test_cursor_rejected_for_a_different_query(self):
        """Replaying a cursor against other SQL would silently return the wrong
        page, so the fingerprint check refuses it."""
        token = cursor_codec.encode("SELECT a FROM t", 20)
        with pytest.raises(GroundingError, match="different query"):
            cursor_codec.decode(token, "SELECT b FROM t")

    def test_malformed_cursor_rejected(self):
        with pytest.raises(GroundingError, match="Malformed"):
            cursor_codec.decode("!!!not-base64!!!", "SELECT 1 FROM t")

    def test_cursor_is_opaque(self):
        """It should read as a token, not an editable offset."""
        token = cursor_codec.encode("SELECT 1 FROM t", 100)
        assert "100" not in token
