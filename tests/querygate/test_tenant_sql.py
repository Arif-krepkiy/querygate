"""Per-engine tenant predicate rewrites and their self-check."""

from __future__ import annotations

import pytest
import sqlglot

from querygate.constants import TENANT_PARAM_NAME
from querygate.warehouse.tenant_sql import (
    to_array_contains,
    to_in_unnest,
    to_pyformat,
)
from querygate.warehouse.types import WarehouseError

CANONICAL = (
    f"SELECT a FROM analytics.orders AS h WHERE h.status = 1 AND h.tenant_id = ANY(:{TENANT_PARAM_NAME})"
)


def _render(sql: str, dialect: str) -> str:
    """Render canonical (Postgres-shaped) SQL the way the pipeline would."""
    return sqlglot.parse_one(sql, dialect="postgres").sql(dialect=dialect)


class TestBigQuery:
    def test_rewrites_any_to_in_unnest(self):
        out = to_in_unnest(_render(CANONICAL, "bigquery"))
        assert f"IN UNNEST(@{TENANT_PARAM_NAME})" in out
        assert "= ANY(" not in out

    def test_keeps_the_rest_of_the_predicate(self):
        out = to_in_unnest(_render(CANONICAL, "bigquery"))
        assert "h.status = 1" in out

    def test_every_governed_scope_is_rewritten(self):
        sql = (
            "SELECT * FROM ("
            f"  SELECT x FROM analytics.a AS h WHERE h.tenant_id = ANY(:{TENANT_PARAM_NAME})"
            ") AS t JOIN analytics.b AS g ON TRUE "
            f"WHERE g.tenant_id = ANY(:{TENANT_PARAM_NAME})"
        )
        out = to_in_unnest(_render(sql, "bigquery"))
        assert out.count("IN UNNEST") == 2
        assert "= ANY(" not in out

    def test_public_query_untouched(self):
        sql = _render("SELECT * FROM analytics.ref_codes", "bigquery")
        assert to_in_unnest(sql) == sql

    def test_result_reparses(self):
        out = to_in_unnest(_render(CANONICAL, "bigquery"))
        assert sqlglot.parse_one(out, dialect="bigquery") is not None


class TestSnowflake:
    def test_rewrites_any_to_array_contains(self):
        out = to_array_contains(_render(CANONICAL, "snowflake"))
        assert "ARRAY_CONTAINS" in out
        assert "PARSE_JSON" in out
        assert "= ANY(" not in out

    def test_binds_through_a_placeholder_not_a_literal(self):
        out = to_array_contains(_render(CANONICAL, "snowflake"))
        assert TENANT_PARAM_NAME in out
        assert "'acme'" not in out

    def test_every_governed_scope_is_rewritten(self):
        sql = (
            "SELECT * FROM ("
            f"  SELECT x FROM analytics.a AS h WHERE h.tenant_id = ANY(:{TENANT_PARAM_NAME})"
            ") AS t JOIN analytics.b AS g ON TRUE "
            f"WHERE g.tenant_id = ANY(:{TENANT_PARAM_NAME})"
        )
        out = to_array_contains(_render(sql, "snowflake"))
        assert out.count("ARRAY_CONTAINS") == 2

    def test_public_query_untouched(self):
        sql = _render("SELECT * FROM analytics.ref_codes", "snowflake")
        assert to_array_contains(sql) == sql


class TestPyformat:
    def test_converts_reserved_names_only(self):
        out = to_pyformat(f"WHERE PARSE_JSON(:{TENANT_PARAM_NAME}) AND x = :not_ours", (TENANT_PARAM_NAME,))
        assert f"%({TENANT_PARAM_NAME})s" in out
        assert ":not_ours" in out


class TestFailsClosed:
    def test_unknown_placeholder_is_not_treated_as_the_tenant_filter(self):
        """A predicate bound to some other parameter must not satisfy the rewrite."""
        sql = _render(
            "SELECT a FROM analytics.orders AS h WHERE h.tenant_id = ANY(:something_else)", "bigquery"
        )
        # Nothing canonical to rewrite -> passes through, still carrying no
        # tenant predicate. The central gate is what rejects this upstream; the
        # rewrite must not invent a guarantee it cannot make.
        assert to_in_unnest(sql) == sql

    def test_self_check_rejects_a_rewrite_that_lost_a_predicate(self, monkeypatch):
        """Simulate a broken rewrite: the output must be re-proved, not trusted."""
        from querygate.warehouse import tenant_sql

        real_condition = sqlglot.condition

        def drop_predicate(fragment, dialect=None, **kwargs):
            # Return a tautology instead of the tenant predicate.
            return real_condition("1 = 1", dialect=dialect)

        monkeypatch.setattr(tenant_sql.sqlglot, "condition", drop_predicate)
        with pytest.raises(WarehouseError, match="failed its own check"):
            tenant_sql.to_in_unnest(_render(CANONICAL, "bigquery"))
