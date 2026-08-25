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


def setup() -> bool:
    """Register collectors. Safe to call twice; returns whether metrics are on."""
    global _ENABLED, _tool_calls, _tool_duration, _result_rows, _response_bytes
    if _ENABLED:
        return True
    try:
        from prometheus_client import Counter, Histogram
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
    _ENABLED = True
    return True


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
