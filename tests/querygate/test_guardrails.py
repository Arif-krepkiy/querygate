"""Static AST guardrails: cartesian products and partition pruning."""

from __future__ import annotations

import pytest

from querygate import config
from querygate.query.guardrails import assert_guardrails
from querygate.query.validation import GroundingError, validate_grounded


@pytest.fixture
def check(catalog):
    """Run the guardrails exactly as the pipeline does."""

    def _check(sql: str) -> None:
        handles = validate_grounded(sql, catalog)
        assert_guardrails(sql, handles, check_reserved=True)

    return _check


class TestCartesianProduct:
    def test_comma_join_rejected(self, check):
        with pytest.raises(GroundingError, match="no condition"):
            check("SELECT o.amount FROM customer_orders o, plan_catalog p")

    def test_cross_join_rejected(self, check):
        with pytest.raises(GroundingError, match="no condition"):
            check("SELECT o.amount FROM customer_orders o CROSS JOIN plan_catalog p")

    def test_constant_on_condition_rejected(self, check):
        # `ON 1 = 1` is a cross join wearing an inner join's clothes.
        with pytest.raises(GroundingError, match="no condition"):
            check("SELECT o.amount FROM customer_orders o JOIN plan_catalog p ON 1 = 1")

    def test_one_sided_on_condition_rejected(self, check):
        # Names only the left side, so it constrains nothing about plan_catalog.
        sql = "SELECT o.amount FROM customer_orders o JOIN plan_catalog p ON o.plan_name = o.region"
        with pytest.raises(GroundingError, match="no condition"):
            check(sql)

    def test_proper_join_accepted(self, check):
        check("SELECT o.amount FROM customer_orders o JOIN plan_catalog p ON o.plan_name = p.plan_name")

    def test_using_accepted(self, check):
        check("SELECT amount FROM customer_orders JOIN plan_catalog USING (plan_name)")

    def test_left_join_accepted(self, check):
        sql = "SELECT o.amount FROM customer_orders o LEFT JOIN plan_catalog p ON o.plan_name = p.plan_name"
        check(sql)

    def test_unqualified_condition_given_benefit_of_the_doubt(self, check):
        # Bare column names: resolving which table they belong to is the
        # engine's job, so the guardrail does not guess.
        check("SELECT amount FROM customer_orders o JOIN plan_catalog p ON plan_name = plan_name")

    def test_single_table_unaffected(self, check):
        check("SELECT amount FROM customer_orders")

    def test_join_inside_subquery_checked(self, check):
        sql = "SELECT total FROM (SELECT o.amount AS total FROM customer_orders o, plan_catalog p) AS sub"
        with pytest.raises(GroundingError, match="no condition"):
            check(sql)


class TestPartitionFilter:
    def setup_method(self):
        self._saved = config.REQUIRE_PARTITION_FILTER
        config.REQUIRE_PARTITION_FILTER = True

    def teardown_method(self):
        config.REQUIRE_PARTITION_FILTER = self._saved

    def test_missing_filter_rejected(self, check):
        with pytest.raises(GroundingError, match="partitioned by 'order_date'"):
            check("SELECT amount FROM customer_orders")

    def test_range_filter_accepted(self, check):
        check("SELECT amount FROM customer_orders WHERE order_date >= '2024-01-01'")

    def test_between_accepted(self, check):
        check("SELECT amount FROM customer_orders WHERE order_date BETWEEN '2024-01-01' AND '2024-02-01'")

    def test_equality_accepted(self, check):
        check("SELECT amount FROM customer_orders WHERE order_date = '2024-01-01'")

    def test_in_list_accepted(self, check):
        check("SELECT amount FROM customer_orders WHERE order_date IN ('2024-01-01', '2024-01-02')")

    def test_qualified_filter_accepted(self, check):
        check("SELECT o.amount FROM customer_orders o WHERE o.order_date >= '2024-01-01'")

    def test_is_not_null_rejected(self, check):
        # Mentions the partition column without pruning a single partition: the
        # shape a model reaches for when it doesn't know the date range.
        with pytest.raises(GroundingError, match="IS NOT NULL"):
            check("SELECT amount FROM customer_orders WHERE order_date IS NOT NULL")

    def test_filter_on_another_column_rejected(self, check):
        with pytest.raises(GroundingError, match="partitioned by 'order_date'"):
            check("SELECT amount FROM customer_orders WHERE region = 'EU'")

    def test_filter_on_wrong_table_rejected(self, check):
        # The predicate narrows the other side of the join, not this table.
        sql = (
            "SELECT o.amount FROM customer_orders o JOIN monthly_revenue m ON o.region = m.region "
            "WHERE m.month >= '2024-01-01'"
        )
        with pytest.raises(GroundingError, match="partitioned by 'order_date'"):
            check(sql)

    def test_unpartitioned_table_unaffected(self, check):
        check("SELECT plan_name FROM plan_catalog")

    def test_each_scope_checked_independently(self, check):
        sql = "SELECT total FROM (SELECT sum(amount) AS total FROM customer_orders) AS sub WHERE total > 0"
        with pytest.raises(GroundingError, match="partitioned by 'order_date'"):
            check(sql)


class TestPartitionFilterDisabled:
    def test_off_by_default(self, check):
        assert config.REQUIRE_PARTITION_FILTER is False
        check("SELECT amount FROM customer_orders")
