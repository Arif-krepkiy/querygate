"""Near-miss suggestions on unknown table and column names."""

from __future__ import annotations

import re

import pytest

from querygate.catalog.suggest import column_hint, similar_models, table_hint
from querygate.query.prepare import prepare_filter_values_query, prepare_query
from querygate.query.validation import GroundingError

SCOPES = frozenset({"acme"})


class TestModelSuggestions:
    def test_misspelling_is_matched(self, catalog):
        assert similar_models(catalog, "custmer_orders") == ["customer_orders"]

    def test_case_is_ignored(self, catalog):
        assert similar_models(catalog, "Customer_Orders") == ["customer_orders"]

    def test_nothing_close_suggests_nothing(self, catalog):
        """A confidently wrong hint is worse than none; the agent will take it."""
        assert similar_models(catalog, "zzzzzzzz") == []
        assert table_hint(catalog, "zzzzzzzz") == ""

    def test_a_semantic_miss_is_not_this_module_s_job(self, catalog):
        """'sales' means monthly_revenue, but that is search's job, not spelling's."""
        assert similar_models(catalog, "sales") == []


class TestColumnSuggestions:
    def test_misspelling_on_the_right_table(self, catalog):
        model = catalog.get_model("customer_orders")
        assert column_hint(catalog, model, "amont") == " Did you mean 'amount'?"

    def test_right_column_wrong_table_names_the_table_and_the_join(self, catalog):
        model = catalog.get_model("customer_orders")
        hint = column_hint(catalog, model, "is_active")
        assert "'active_customers'" in hint
        assert "join on customer_orders.customer_id = active_customers.customer_id" in hint

    def test_join_hint_is_found_in_either_direction(self, catalog):
        """plan_catalog declares no joins; customer_orders declares the one to it."""
        model = catalog.get_model("plan_catalog")
        assert "join on plan_catalog.plan_name = customer_orders.plan_name" in column_hint(
            catalog, model, "status"
        )

    def test_no_declared_join_still_names_the_table(self, catalog):
        model = catalog.get_model("monthly_revenue")
        hint = column_hint(catalog, model, "tier")
        assert "'tier' exists on 'plan_catalog'." == hint.strip()

    def test_spelling_wins_over_location(self, catalog):
        """A typo on this table is likelier, and the cheaper fix, so report it first."""
        model = catalog.get_model("customer_orders")
        assert column_hint(catalog, model, "regio").startswith(" Did you mean 'region'?")

    def test_unknown_everywhere_suggests_nothing(self, catalog):
        model = catalog.get_model("customer_orders")
        assert column_hint(catalog, model, "zzzzzzzz") == ""


class TestHintsReachTheAgent:
    def test_unknown_table_error_carries_the_hint(self, catalog):
        with pytest.raises(GroundingError, match="Did you mean 'customer_orders'"):
            prepare_query("SELECT amount FROM custmer_orders", catalog, SCOPES)

    def test_unknown_column_error_carries_the_hint(self, catalog):
        with pytest.raises(GroundingError, match="Did you mean 'amount'"):
            prepare_query("SELECT o.amont FROM customer_orders o", catalog, SCOPES)

    def test_cross_table_column_error_carries_the_join(self, catalog):
        with pytest.raises(GroundingError, match=re.escape("join on customer_orders.customer_id")):
            prepare_query("SELECT o.is_active FROM customer_orders o", catalog, SCOPES)

    def test_filter_values_column_error_carries_the_hint(self, catalog):
        with pytest.raises(GroundingError, match="Did you mean 'region'"):
            prepare_filter_values_query("customer_orders", "reigon", catalog, SCOPES)

    def test_filter_values_table_error_carries_the_hint(self, catalog):
        with pytest.raises(GroundingError, match="Did you mean 'plan_catalog'"):
            prepare_filter_values_query("plan_catalogue", "plan_name", catalog, SCOPES)

    def test_the_original_guidance_survives(self, catalog):
        """A hint is added to the message, never in place of what to do next."""
        with pytest.raises(GroundingError, match="qg_describe_model"):
            prepare_query("SELECT o.amont FROM customer_orders o", catalog, SCOPES)
