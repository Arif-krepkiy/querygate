"""One decorator that traces, times and counts a tool call."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec

from querygate.log_setup import get_logger
from querygate.obs import metrics, tracing
from querygate.obs.tracing import current_span, set_query_attributes, tool_span

_P = ParamSpec("_P")
_log = get_logger("querygate.obs")


def setup() -> None:
    """Initialise tracing and metrics. Called once at startup."""
    tracing.setup()
    metrics.setup()


def _outcome_and_rows(payload: str) -> tuple[str, int | None]:
    """Classify a tool result. Errors carry a `kind`; successes may carry rows."""
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return "ok", None
    if not isinstance(parsed, dict):
        return "ok", None
    if "error" in parsed:
        return str(parsed.get("kind", "error")), None
    rows = parsed.get("row_count")
    return "ok", int(rows) if isinstance(rows, int) else None


def observed(tool: str) -> Callable[[Callable[_P, Awaitable[str]]], Callable[_P, Awaitable[str]]]:
    """Trace, time and count one tool call."""

    def decorate(func: Callable[_P, Awaitable[str]]) -> Callable[_P, Awaitable[str]]:
        @wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> str:
            started = time.perf_counter()
            with tool_span(tool) as span:
                try:
                    payload = await func(*args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    metrics.record_call(tool, "exception", time.perf_counter() - started, None, 0)
                    raise
                outcome, rows = _outcome_and_rows(payload)
                duration = time.perf_counter() - started
                span.set_attribute("qg.outcome", outcome)
                if rows is not None:
                    span.set_attribute("qg.rows", rows)
                span.set_attribute("qg.response_bytes", len(payload))
                metrics.record_call(tool, outcome, duration, rows, len(payload))
                return payload

        return wrapper

    return decorate


__all__ = [
    "current_span",
    "metrics",
    "observed",
    "set_query_attributes",
    "setup",
    "tool_span",
    "tracing",
]
