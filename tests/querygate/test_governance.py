"""The governance matrix: where the tenant filter is injected, and where it is not."""

from __future__ import annotations

import pytest

from querygate.constants import TENANT_PARAM_NAME
from querygate.query.governance import (
    GovernanceError,
    assert_tenant_filter_present,
    inject_tenant_filter,
)
from querygate.query.prepare import prepare_query


def _governed_sql(catalog, sql):
    return inject_tenant_filter(sql, catalog)


class TestInjection:
    def test_single_governed_table(self, catalog):
        out = _governed_sql(catalog, "SELECT region FROM customer_orders")
        assert f"tenant_id = ANY(%({TENANT_PARAM_NAME})s)" in out
        assert_tenant_filter_present(out, catalog)

    def test_alias_uses_handle(self, catalog):
        out = _governed_sql(catalog, "SELECT o.region FROM customer_orders o")
        assert "o.tenant_id = ANY" in out
        assert_tenant_filter_present(out, catalog)

    def test_join_two_governed_each_filtered(self, catalog):
        sql = "SELECT a.region FROM customer_orders a JOIN monthly_revenue m ON a.region = m.region"
        out = _governed_sql(catalog, sql)
        assert "a.tenant_id = ANY" in out
        assert "m.tenant_id = ANY" in out
        assert_tenant_filter_present(out, catalog)

    def test_mixed_governed_and_public_only_governed_filtered(self, catalog):
        sql = "SELECT o.region FROM customer_orders o JOIN plan_catalog p ON o.plan_name = p.plan_name"
        out = _governed_sql(catalog, sql)
        assert out.count("= ANY") == 1  # only the governed side
        assert "o.tenant_id = ANY" in out

    def test_public_only_gets_no_filter(self, catalog):
        out = _governed_sql(catalog, "SELECT plan_name FROM plan_catalog")
        assert "ANY" not in out

    def test_subquery_scope_filtered(self, catalog):
        sql = "SELECT * FROM (SELECT region FROM customer_orders) s"
        out = _governed_sql(catalog, sql)
        assert "tenant_id = ANY" in out
        assert_tenant_filter_present(out, catalog)

    def test_cte_body_filtered_name_not_a_table(self, catalog):
        sql = "WITH r AS (SELECT tenant_id, amount FROM customer_orders) SELECT sum(amount) FROM r"
        out = _governed_sql(catalog, sql)
        assert "customer_orders.tenant_id = ANY" in out
        assert_tenant_filter_present(out, catalog)

    def test_injection_idempotent(self, catalog):
        once = _governed_sql(catalog, "SELECT region FROM customer_orders")
        twice = _governed_sql(catalog, once)
        assert_tenant_filter_present(twice, catalog)


class TestAssertRejects:
    def test_missing_filter(self, catalog):
        with pytest.raises(GovernanceError, match="customer_orders"):
            assert_tenant_filter_present("SELECT region FROM customer_orders", catalog)

    def test_or_ed_tautology_rejected(self, catalog):
        sql = (
            f"SELECT region FROM customer_orders "
            f"WHERE customer_orders.tenant_id = ANY(:{TENANT_PARAM_NAME}) OR 1 = 1"
        )
        with pytest.raises(GovernanceError):
            assert_tenant_filter_present(sql, catalog)

    def test_wrong_parameter_rejected(self, catalog):
        sql = "SELECT region FROM customer_orders WHERE customer_orders.tenant_id = ANY(:not_the_param)"
        with pytest.raises(GovernanceError):
            assert_tenant_filter_present(sql, catalog)


class TestFailClosed:
    def test_governed_empty_scope_rejected(self, catalog):
        with pytest.raises(GovernanceError, match="fail-closed"):
            prepare_query("SELECT * FROM monthly_revenue", catalog, frozenset())

    def test_public_empty_scope_allowed(self, catalog):
        prepared = prepare_query("SELECT plan_name FROM plan_catalog", catalog, frozenset())
        assert prepared.tenant_scopes == ()
