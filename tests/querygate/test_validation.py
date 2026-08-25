"""Read-only validation, grounding, and the qualify/limit steps."""

from __future__ import annotations

import pytest

from querygate.query.limits import clamp_row_limit, enforce_row_limit
from querygate.query.prepare import prepare_query
from querygate.query.qualify import qualify_tables
from querygate.query.validation import GroundingError, validate_grounded


class TestReadOnly:
    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM customer_orders",
            "UPDATE customer_orders SET amount = 0",
            "INSERT INTO customer_orders VALUES (1)",
            "DROP TABLE customer_orders",
            "SELECT 1; SELECT 2",
            "",
        ],
    )
    def test_rejects_non_select(self, catalog, sql):
        with pytest.raises(GroundingError):
            validate_grounded(sql, catalog)

    def test_rejects_reserved_placeholder(self, catalog):
        with pytest.raises(GroundingError, match="reserved"):
            validate_grounded("SELECT region FROM customer_orders WHERE x = :qg_tenant_scopes", catalog)


class TestGrounding:
    def test_unknown_table(self, catalog):
        with pytest.raises(GroundingError, match="unknown table"):
            validate_grounded("SELECT * FROM ghosts", catalog)

    def test_hallucinated_qualified_column(self, catalog):
        with pytest.raises(GroundingError, match="not in the catalog"):
            validate_grounded("SELECT o.nonexistent FROM customer_orders o", catalog)

    def test_unqualified_columns_allowed(self, catalog):
        # CTE outputs / aliases are unqualified, so not checked.
        handles = validate_grounded(
            "SELECT region, sum(amount) FROM customer_orders GROUP BY region", catalog
        )
        assert "customer_orders" in {m.name for m in handles.values()}


class TestRowLimit:
    def test_clamp(self):
        assert clamp_row_limit(None) == 100
        assert clamp_row_limit(999999) == 1000
        assert clamp_row_limit(5) == 5
        assert clamp_row_limit(0) == 1

    def test_injected_when_absent(self):
        assert "LIMIT 100" in enforce_row_limit("SELECT 1", 100)

    def test_clamped_down(self):
        assert "LIMIT 50" in enforce_row_limit("SELECT 1 LIMIT 9999", 50)

    def test_smaller_untouched(self):
        assert "LIMIT 10" in enforce_row_limit("SELECT 1 LIMIT 10", 100)


class TestQualify:
    def test_bare_name_qualified(self, catalog):
        out = qualify_tables("SELECT * FROM customer_orders", catalog)
        assert '"analytics"."customer_orders"' in out

    def test_alias_preserved(self, catalog):
        out = qualify_tables("SELECT o.region FROM customer_orders o", catalog)
        assert "AS o" in out


class TestPrepareEndToEnd:
    def test_full_pipeline(self, catalog):
        p = prepare_query(
            "SELECT region, sum(amount) FROM customer_orders GROUP BY region", catalog, frozenset({"acme"})
        )
        assert '"analytics"."customer_orders"' in p.sql
        assert "LIMIT" in p.sql
        assert "tenant_id = ANY" in p.sql
        assert p.bind_params()["qg_tenant_scopes"] == ["acme"]
