"""DuckDB warehouse adapter. In-process, so no server and no credentials."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from querygate import config
from querygate.log_setup import get_logger
from querygate.warehouse.types import CostEstimate, QueryResult, WarehouseError

if TYPE_CHECKING:
    import duckdb

    from querygate.query.prepare import PreparedQuery

_log = get_logger("querygate.warehouse.duckdb")

# DuckDB renders its plan as a box diagram annotating each operator with its
# estimated cardinality, e.g. "~1,000 rows" (older builds used "EC: 1000"). Both
# forms are accepted so a driver upgrade does not silently zero the estimate.
_ESTIMATED_ROWS = re.compile(r"~\s*([\d,]+)\s*rows|EC:\s*(\d+)")

_connection: duckdb.DuckDBPyConnection | None = None


def _connect() -> duckdb.DuckDBPyConnection:
    """One process-wide connection; DuckDB serialises access internally.

    Cursors are taken per call so concurrent queries do not share state.
    """
    global _connection
    if _connection is None:
        import duckdb

        path = config.DUCKDB_PATH
        try:
            # Prefer read-only: defence in depth behind the AST validation.
            # An in-memory database cannot be opened read-only (there would be
            # nothing to read), so that case falls through.
            _connection = duckdb.connect(path, read_only=(path != ":memory:"))
        except Exception as exc:
            _log.warning("read-only open failed (%s); opening read-write", exc)
            _connection = duckdb.connect(path)
        _log.info("DuckDB connected: %s", path)
    return _connection


def reset_connection() -> None:
    """Drop the cached handle. Used by tests that swap databases."""
    global _connection
    if _connection is not None:
        _connection.close()
    _connection = None


def _safe(exc: Exception) -> WarehouseError:
    """Map a DuckDB error to an actionable message that leaks no internals."""
    name = type(exc).__name__
    if "OutOfMemory" in name:
        return WarehouseError("The query ran out of memory. Aggregate further or select fewer columns.")
    if "Catalog" in name or "Binder" in name:
        return WarehouseError("A referenced table or column was not found. It may have been renamed.")
    if "Permission" in name or "ReadOnly" in name:
        return WarehouseError("The warehouse is read-only and rejected this operation.")
    # Conversion/syntax errors can echo values from the data. Log, don't return.
    _log.warning("duckdb error: %s: %s", name, str(exc)[:300])
    return WarehouseError("The warehouse rejected the query. Check table/column names and types, then retry.")


def _run_estimate(prepared: PreparedQuery) -> CostEstimate:
    con = _connect().cursor()
    try:
        con.execute(f"EXPLAIN {prepared.sql}", prepared.bind_params())
        plan = "\n".join(str(part) for row in con.fetchall() for part in row)
    except Exception as exc:
        raise _safe(exc) from exc
    # Take the widest operator estimate: the most rows the plan expects to
    # touch, which is what a ceiling should be compared against.
    estimates = [
        int(value.replace(",", "")) for match in _ESTIMATED_ROWS.findall(plan) for value in match if value
    ]
    return CostEstimate(plan_cost=float(max(estimates, default=0)))


def _run_execute(prepared: PreparedQuery) -> QueryResult:
    con = _connect().cursor()
    try:
        con.execute(prepared.sql, prepared.bind_params())
        columns = [d[0] for d in con.description or []]
        rows = con.fetchmany(prepared.row_limit)
    except Exception as exc:
        raise _safe(exc) from exc
    dict_rows: list[dict[str, Any]] = [dict(zip(columns, row, strict=False)) for row in rows]
    return QueryResult(columns=columns, rows=dict_rows, row_count=len(dict_rows))


async def estimate(prepared: PreparedQuery) -> CostEstimate:
    return await asyncio.to_thread(_run_estimate, prepared)


async def execute(prepared: PreparedQuery) -> QueryResult:
    return await asyncio.to_thread(_run_execute, prepared)
