"""Postgres warehouse adapter: EXPLAIN to estimate, then execute read-only."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import psycopg
from psycopg.rows import dict_row

from querygate import config
from querygate.log_setup import get_logger
from querygate.warehouse.types import CostEstimate, QueryResult, WarehouseError

if TYPE_CHECKING:
    from querygate.query.prepare import PreparedQuery

_log = get_logger("querygate.warehouse")


def _connect() -> psycopg.Connection:
    conn = psycopg.connect(config.PG_DSN, row_factory=dict_row)
    conn.read_only = True
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(config.STATEMENT_TIMEOUT_MS)}")
    return conn


def _safe(exc: Exception) -> WarehouseError:
    """Map a driver error to an actionable message that leaks no internals."""
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return WarehouseError(
            "The query took too long and was cancelled. Add a filter (often a date or partition) "
            "or select fewer columns."
        )
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return WarehouseError("Access denied by the warehouse for one of the requested tables.")
    if isinstance(exc, psycopg.errors.UndefinedTable | psycopg.errors.UndefinedColumn):
        return WarehouseError("A referenced table or column was not found. It may have been renamed.")
    # Syntax / type errors can echo values from the data. Log server-side only.
    _log.warning("warehouse error: %s: %s", type(exc).__name__, str(exc)[:300])
    return WarehouseError("The warehouse rejected the query. Check table/column names and types, then retry.")


def _run_estimate(prepared: PreparedQuery) -> CostEstimate:
    with _connect() as conn, conn.cursor() as cur:
        try:
            cur.execute(f"EXPLAIN (FORMAT JSON) {prepared.sql}", prepared.bind_params())
            plan = cur.fetchone()["QUERY PLAN"]  # type: ignore[index]
        except psycopg.Error as exc:
            raise _safe(exc) from exc
    if isinstance(plan, str):
        plan = json.loads(plan)
    total_cost = float(plan[0]["Plan"]["Total Cost"])
    return CostEstimate(plan_cost=total_cost)


def _run_execute(prepared: PreparedQuery) -> QueryResult:
    with _connect() as conn, conn.cursor() as cur:
        try:
            cur.execute(prepared.sql, prepared.bind_params())
            rows = cur.fetchmany(prepared.row_limit)
            columns = [desc.name for desc in cur.description or []]
        except psycopg.Error as exc:
            raise _safe(exc) from exc
    return QueryResult(columns=columns, rows=list(rows), row_count=len(rows))


async def estimate(prepared: PreparedQuery) -> CostEstimate:
    return await asyncio.to_thread(_run_estimate, prepared)


async def execute(prepared: PreparedQuery) -> QueryResult:
    return await asyncio.to_thread(_run_execute, prepared)
