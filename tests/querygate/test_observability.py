"""Tracing and metrics, including span redaction."""

from __future__ import annotations

import pytest

from querygate import config
from querygate.obs import metrics, observed
from querygate.obs.tracing import set_query_attributes


class RecordingSpan:
    """Captures attributes the way a real span would receive them."""

    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.exceptions: list[BaseException] = []

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self.exceptions.append(exc)

    def set_status(self, *_args: object) -> None: ...


class TestRedaction:
    def test_sql_and_tenants_withheld_by_default(self, monkeypatch):
        monkeypatch.setattr(config, "TRACE_SENSITIVE", False)
        span = RecordingSpan()
        set_query_attributes(
            span,
            tables=("customer_orders",),
            tenant_count=1,
            plan_cost=42.0,
            sql="SELECT * FROM customer_orders WHERE tenant_id = ANY(...)",
            tenant_scopes=("acme",),
        )
        assert "qg.sql" not in span.attributes
        assert "qg.tenant_scopes" not in span.attributes
        # No attribute value may contain the tenant identifier anywhere.
        assert all("acme" not in str(v) for v in span.attributes.values())

    def test_structure_is_kept(self, monkeypatch):
        """Redaction must not make traces useless; shape still comes through."""
        monkeypatch.setattr(config, "TRACE_SENSITIVE", False)
        span = RecordingSpan()
        set_query_attributes(span, tables=("customer_orders",), tenant_count=2, rows=17, plan_cost=9.5)
        assert span.attributes["qg.tables"] == "customer_orders"
        assert span.attributes["qg.tenant_scope_count"] == 2
        assert span.attributes["qg.rows"] == 17
        assert span.attributes["qg.plan_cost"] == 9.5

    def test_opt_in_reveals_sensitive_fields(self, monkeypatch):
        monkeypatch.setattr(config, "TRACE_SENSITIVE", True)
        span = RecordingSpan()
        set_query_attributes(span, sql="SELECT 1", tenant_scopes=("acme",))
        assert span.attributes["qg.sql"] == "SELECT 1"
        assert span.attributes["qg.tenant_scopes"] == "acme"

    def test_tenant_count_not_identity(self, monkeypatch):
        """A multi-tenant caller is visible as a count, not as a customer list."""
        monkeypatch.setattr(config, "TRACE_SENSITIVE", False)
        span = RecordingSpan()
        set_query_attributes(span, tenant_count=3, tenant_scopes=("acme", "globex", "initech"))
        assert span.attributes["qg.tenant_scope_count"] == 3
        assert "globex" not in str(span.attributes)


class TestObservedDecorator:
    async def test_records_success_and_row_count(self):
        pytest.importorskip("prometheus_client")
        assert metrics.setup()

        @observed("unit_test_ok")
        async def tool() -> str:
            return '{"row_count":5,"rows":[]}'

        assert await tool() == '{"row_count":5,"rows":[]}'

    async def test_classifies_error_kind(self):
        """A governance rejection is an outcome to count, not an error to lose.
        The rejection rate is the most interesting signal this server emits."""
        from querygate.obs import _outcome_and_rows

        outcome, rows = _outcome_and_rows('{"error":"nope","kind":"governance"}')
        assert outcome == "governance"
        assert rows is None

    async def test_classifies_success(self):
        from querygate.obs import _outcome_and_rows

        assert _outcome_and_rows('{"row_count":3}') == ("ok", 3)

    async def test_non_json_payload_is_not_fatal(self):
        from querygate.obs import _outcome_and_rows

        assert _outcome_and_rows("plain text") == ("ok", None)

    async def test_exception_propagates(self):
        @observed("unit_test_boom")
        async def tool() -> str:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await tool()


class TestMetricsEndpoint:
    def test_render_produces_exposition_format(self):
        pytest.importorskip("prometheus_client")
        assert metrics.setup()
        metrics.record_call("run_query", "ok", 0.01, 5, 1024)
        payload, content_type = metrics.render()
        assert b"querygate_tool_calls_total" in payload
        assert "text/plain" in content_type

    def test_disabled_metrics_are_a_noop(self, monkeypatch):
        """Without prometheus_client the recorder must stay callable."""
        monkeypatch.setattr(metrics, "_ENABLED", False)
        metrics.record_call("run_query", "ok", 0.01, 5, 1024)  # must not raise
