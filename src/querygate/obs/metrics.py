"""Prometheus metrics."""

from __future__ import annotations

from typing import Any

from querygate.log_setup import get_logger

_log = get_logger("querygate.obs")

_ENABLED = False
_tool_calls: Any = None
_tool_duration: Any = None
_result_rows: Any = None
_response_bytes: Any = None
_query_cost: Any = None
_cost_refusals: Any = None
_catalog_models: Any = None
_catalog_synced: Any = None


def setup() -> bool:
    """Register collectors. Safe to call twice; returns whether metrics are on."""
    global _ENABLED, _tool_calls, _tool_duration, _result_rows, _response_bytes
    global _query_cost, _cost_refusals, _catalog_models, _catalog_synced
    if _ENABLED:
        return True
    try:
        from prometheus_client import Counter, Gauge, Histogram
    except ImportError:
        _log.info("prometheus_client not installed; metrics disabled")
        return False

    _tool_calls = Counter(
        "querygate_tool_calls_total",
        "MCP tool invocations by outcome.",
        ["tool", "outcome"],
    )
    _tool_duration = Histogram(
        "querygate_tool_duration_seconds",
        "Wall-clock duration of an MCP tool call.",
        ["tool"],
    )
    _result_rows = Histogram(
        "querygate_result_rows",
        "Rows returned to the agent.",
        ["tool"],
        buckets=(1, 10, 25, 50, 100, 250, 500, 1000),
    )
    _response_bytes = Histogram(
        "querygate_response_bytes",
        "Payload size returned to the agent, a proxy for context spend.",
        ["tool"],
        buckets=(512, 2048, 8192, 32768, 131072, 524288),
    )
    _query_cost = Histogram(
        "querygate_query_cost",
        "Pre-execution cost estimate. Unit is engine-specific: BigQuery and Snowflake "
        "bytes, Postgres planner cost, DuckDB estimated rows.",
        ["engine"],
        buckets=(1e3, 1e5, 1e7, 1e8, 1e9, 1e10, 1e11, 1e12),
    )
    _cost_refusals = Counter(
        "querygate_cost_refusals_total",
        "Queries refused because the estimate exceeded the ceiling, i.e. what the gate prevented.",
        ["engine"],
    )
    _catalog_models = Gauge(
        "querygate_catalog_models",
        "Models in the loaded catalog. A drop means a bad publish, and answers degrade "
        "before anything errors.",
    )
    _catalog_synced = Gauge(
        "querygate_catalog_synced_timestamp_seconds",
        "Unix time of the last successful catalog load; alert on its age.",
    )
    _ENABLED = True
    return True


def record_cost(engine: str, cost: float, *, refused: bool) -> None:
    if not _ENABLED:
        return
    _query_cost.labels(engine=engine).observe(cost)
    if refused:
        _cost_refusals.labels(engine=engine).inc()


def record_catalog(models: int, synced_at: float) -> None:
    if not _ENABLED:
        return
    _catalog_models.set(models)
    _catalog_synced.set(synced_at)


def record_call(tool: str, outcome: str, duration: float, rows: int | None, size: int) -> None:
    if not _ENABLED:
        return
    _tool_calls.labels(tool=tool, outcome=outcome).inc()
    _tool_duration.labels(tool=tool).observe(duration)
    _response_bytes.labels(tool=tool).observe(size)
    if rows is not None:
        _result_rows.labels(tool=tool).observe(rows)


def render() -> tuple[bytes, str]:
    """Return the exposition payload and its content type."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return generate_latest(), CONTENT_TYPE_LATEST
