"""Pipeline stage recording."""

from __future__ import annotations

import pytest

from querygate.query.governance import GovernanceError
from querygate.query.prepare import prepare_query, trace_query
from querygate.query.validation import GroundingError

SCOPES = frozenset({"acme"})


class TestTraceMatchesReality:
    def test_final_stage_is_what_actually_executes(self, catalog):
        """The whole point: a trace shows the real pipeline, not a retelling."""
        sql = "SELECT region, sum(amount) FROM customer_orders GROUP BY region"
        stages, prepared, error = trace_query(sql, catalog, SCOPES)
        assert error is None
        assert stages[-1].sql == prepared.sql == prepare_query(sql, catalog, SCOPES).sql

    def test_recording_does_not_change_the_outcome(self, catalog):
        sql = "SELECT customer_name, amount FROM customer_orders"
        _, prepared, _ = trace_query(sql, catalog, SCOPES)
        assert prepared.sql == prepare_query(sql, catalog, SCOPES).sql
        assert prepared.bind_params() == prepare_query(sql, catalog, SCOPES).bind_params()

    def test_every_stage_is_reported(self, catalog):
        stages, _, _ = trace_query("SELECT amount FROM customer_orders", catalog, SCOPES)
        assert [s.name for s in stages] == [
            "validate",
            "guard",
            "qualify",
            "limit",
            "offset",
            "mask",
            "govern",
            "assert",
        ]


class TestChangedFlag:
    def _stage(self, catalog, sql, name, scopes=SCOPES):
        stages, _, _ = trace_query(sql, catalog, scopes)
        return next(s for s in stages if s.name == name)

    def test_checks_rewrite_nothing(self, catalog):
        """validate/guard/assert prove things; they must not touch the SQL."""
        sql = "SELECT amount FROM customer_orders"
        for name in ("validate", "guard", "assert"):
            assert not self._stage(catalog, sql, name).changed

    def test_governance_marked_changed_on_a_governed_table(self, catalog):
        stage = self._stage(catalog, "SELECT amount FROM customer_orders", "govern")
        assert stage.changed
        assert "qg_tenant_scopes" in stage.sql

    def test_governance_marked_unchanged_on_a_public_table(self, catalog):
        stage = self._stage(catalog, "SELECT plan_name FROM plan_catalog", "govern")
        assert not stage.changed
        assert "qg_tenant_scopes" not in stage.sql

    def test_masking_marked_changed_only_when_pii_is_selected(self, catalog):
        assert self._stage(catalog, "SELECT customer_name FROM customer_orders", "mask").changed
        assert not self._stage(catalog, "SELECT amount FROM customer_orders", "mask").changed


class TestRefusals:
    def test_refusal_is_returned_with_the_stages_that_ran(self, catalog):
        """A refusal is the interesting case; the stages before it are the context."""
        sql = "SELECT o.amount, p.tier FROM customer_orders o, plan_catalog p"
        stages, prepared, error = trace_query(sql, catalog, SCOPES)
        assert prepared is None
        assert isinstance(error, GroundingError)
        assert [s.name for s in stages] == ["validate"]

    def test_fail_closed_refusal_is_traced(self, catalog):
        stages, prepared, error = trace_query("SELECT sum(amount) FROM customer_orders", catalog, frozenset())
        assert prepared is None
        assert isinstance(error, GovernanceError)
        assert [s.name for s in stages] == ["validate", "guard"]

    def test_unknown_table_refused_before_any_stage(self, catalog):
        stages, prepared, error = trace_query("SELECT * FROM nope", catalog, SCOPES)
        assert prepared is None
        assert isinstance(error, GroundingError)
        assert stages == []

    def test_an_unexpected_error_still_propagates(self, catalog):
        """Only the two documented refusal types are captured. A bug must not
        be reported to the caller as a governance decision."""
        with pytest.raises(TypeError):
            trace_query("SELECT amount FROM customer_orders", catalog, SCOPES, limit="not an int")


class TestNullRecorder:
    def test_the_normal_path_records_nothing(self, catalog):
        """prepare_query keeps no copies of intermediate SQL."""
        from querygate.query.trace import NULL_RECORDER, NullRecorder

        assert isinstance(NULL_RECORDER, NullRecorder)
        assert NULL_RECORDER.record("a", "b", "c") is None
