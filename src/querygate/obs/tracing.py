"""OpenTelemetry tracing. Spans are redacted unless QG_TRACE_SENSITIVE is set."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from querygate import config
from querygate.log_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

_log = get_logger("querygate.obs")

_tracer: Any = None


def setup() -> bool:
    """Initialise the tracer provider. Returns whether tracing is on."""
    global _tracer
    if _tracer is not None:
        return True
    if not config.TRACING_ENABLED:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        _log.warning("QG_TRACING_ENABLED is set but opentelemetry is missing (install querygate[otel])")
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": config.SERVICE_NAME}))
    if config.OTLP_ENDPOINT:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=config.OTLP_ENDPOINT)))
        _log.info("tracing → %s", config.OTLP_ENDPOINT)
    else:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        _log.info("tracing → console (set QG_OTLP_ENDPOINT to export)")

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("querygate")
    return True


@contextmanager
def tool_span(tool: str) -> Iterator[Any]:
    """Span around one tool call. A no-op object when tracing is off, so call
    sites never branch on whether observability is configured."""
    if _tracer is None:
        yield _NullSpan()
        return
    with _tracer.start_as_current_span(f"mcp.tool/{tool}") as span:
        span.set_attribute("qg.tool", tool)
        yield span


def set_query_attributes(
    span: Any,
    *,
    tables: tuple[str, ...] = (),
    tenant_count: int | None = None,
    rows: int | None = None,
    plan_cost: float | None = None,
    sql: str | None = None,
    tenant_scopes: tuple[str, ...] = (),
) -> None:
    """Attach query facts to a span, redacting identity and query text.

    ``tables`` is included: an operator debugging a slow call needs to know
    which models were touched, and model names are schema, not data. ``sql``
    and ``tenant_scopes`` are withheld unless explicitly enabled.
    """
    if tables:
        span.set_attribute("qg.tables", ",".join(tables))
    if tenant_count is not None:
        # The *number* of scopes is useful (single vs multi-tenant caller);
        # the values identify a customer, so they stay out.
        span.set_attribute("qg.tenant_scope_count", tenant_count)
    if rows is not None:
        span.set_attribute("qg.rows", rows)
    if plan_cost is not None:
        span.set_attribute("qg.plan_cost", plan_cost)

    if config.TRACE_SENSITIVE:
        if sql:
            span.set_attribute("qg.sql", sql)
        if tenant_scopes:
            span.set_attribute("qg.tenant_scopes", ",".join(tenant_scopes))


def current_span() -> Any:
    """The span the ``observed`` decorator opened, or a no-op stand-in."""
    if _tracer is None:
        return _NullSpan()
    from opentelemetry import trace

    return trace.get_current_span()


class _NullSpan:
    """Accepts every span call and does nothing."""

    def set_attribute(self, *_args: object, **_kwargs: object) -> None: ...
    def record_exception(self, *_args: object, **_kwargs: object) -> None: ...
    def set_status(self, *_args: object, **_kwargs: object) -> None: ...
