"""Snowflake adapter. Also the engine that can run a query as the caller's role."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

from querygate import config
from querygate.constants import RESERVED_TOKENS, TENANT_PARAM_NAME
from querygate.log_setup import get_logger
from querygate.warehouse.tenant_sql import to_array_contains, to_pyformat
from querygate.warehouse.types import CostEstimate, QueryResult, WarehouseError

if TYPE_CHECKING:
    from snowflake.connector import SnowflakeConnection

    from querygate.query.prepare import PreparedQuery

_log = get_logger("querygate.warehouse.snowflake")

# Unquoted Snowflake identifier. Role names come from operator config, not from
# a token or the agent, but they still reach SQL as text.
_SAFE_ROLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _pin_role(conn: SnowflakeConnection, role: str) -> None:
    """Pin the session to exactly one role, then prove it took effect.

    Two things make this more than ``USE ROLE``:

    * **Secondary roles are disabled first.** The service user must be granted every
      mapped role for this design to work at all, and if its
      ``DEFAULT_SECONDARY_ROLES`` is ``ALL`` (common), the session activates all of
      them regardless of the primary role. The caller would then read every
      audience's views while the connection *looks* correctly scoped: wrong rows,
      no error. ``USE SECONDARY ROLES NONE`` is what actually narrows it.
    * **The result is verified, not assumed.** ``CURRENT_ROLE()`` is read back
      and compared, so a silent fallback to a default role fails closed here
      rather than answering as somebody else.
    """
    if not _SAFE_ROLE.match(role):
        # Operator-supplied, never user input, but an identifier reaching SQL
        # unquoted deserves a whitelist rather than trust.
        msg = f"Configured warehouse role '{role}' is not a valid Snowflake identifier."
        raise WarehouseError(msg)
    with conn.cursor() as cur:
        cur.execute("USE SECONDARY ROLES NONE")
        cur.execute(f"USE ROLE {role}")
        cur.execute("SELECT CURRENT_ROLE()")
        row = cur.fetchone()
    active = str(row[0]) if row else ""
    if active.upper() != role.upper():
        msg = f"Session is running as '{active}' but this caller requires '{role}'. Refusing to execute."
        raise WarehouseError(msg)


def _connect(role: str | None = None) -> SnowflakeConnection:
    """A connection per call, closed by the caller.

    Snowflake connections are not thread-safe to share, and a pool would have to
    be invalidated on token refresh; the connect cost is small next to the query.
    ``statement_timeout_in_seconds`` is set on the session because on this engine
    the timeout, not a byte ceiling, is what bounds spend.

    ``role`` is the caller's warehouse role in warehouse-enforced mode. Opening
    the session under it is the whole mechanism there: Snowflake's grants, not
    this server, decide which views the query can read. The connecting user must
    have been granted every mapped role. When it is None (inject mode) the
    configured service role is used, and the tenant predicate does the work.
    """
    import snowflake.connector

    conn = snowflake.connector.connect(
        account=config.SF_ACCOUNT,
        user=config.SF_USER,
        password=config.SF_PASSWORD or None,
        authenticator=config.SF_AUTHENTICATOR or None,
        role=role or config.SF_ROLE or None,
        warehouse=config.SF_WAREHOUSE or None,
        database=config.SF_DATABASE or None,
        schema=config.SF_SCHEMA or None,
        session_parameters={
            "STATEMENT_TIMEOUT_IN_SECONDS": max(1, int(config.STATEMENT_TIMEOUT_MS / 1000)),
        },
    )
    if role:
        try:
            _pin_role(conn, role)
        except Exception:
            conn.close()
            raise
    return conn


def _bind(prepared: PreparedQuery) -> dict[str, Any]:
    """Bound values for pyformat placeholders; the scope list becomes JSON."""
    params: dict[str, Any] = {}
    for name, value in prepared.bind_params().items():
        params[name] = json.dumps(list(value)) if name == TENANT_PARAM_NAME else value
    return params


def _prepare_sql(prepared: PreparedQuery) -> str:
    rewritten = to_array_contains(prepared.sql, dialect=config.SQL_DIALECT)
    return to_pyformat(rewritten, RESERVED_TOKENS)


def _safe(exc: Exception) -> WarehouseError:
    """Map a driver error to an actionable message that leaks no internals."""
    code = getattr(exc, "errno", None)
    name = type(exc).__name__
    # 604/608: statement cancelled or timed out. 002003: object does not exist.
    # 003001/090105: insufficient privileges / no active warehouse.
    if code in {604, 608}:
        return WarehouseError(
            "The query took too long and was cancelled. Add a filter (often a date or partition) "
            "or select fewer columns."
        )
    text = str(exc)
    if "does not exist" in text or "invalid identifier" in text.lower():
        return WarehouseError("A referenced table or column was not found. It may have been renamed.")
    if "not authorized" in text.lower() or "insufficient privileges" in text.lower():
        return WarehouseError("Access denied by the warehouse for one of the requested tables.")
    _log.warning("snowflake error: %s: %s", name, text[:300])
    return WarehouseError("The warehouse rejected the query. Check table/column names and types, then retry.")


def _explain_bytes(plan_json: str) -> float:
    """Pull the scanned-bytes estimate out of ``EXPLAIN USING JSON`` output.

    Shape varies by Snowflake release, so every lookup is defensive: a plan we
    cannot read yields 0 (no estimate) rather than an exception, and the row
    limit plus the statement timeout remain the real guards.
    """
    try:
        plan = json.loads(plan_json)
    except (TypeError, ValueError):
        return 0.0
    stats = plan.get("GlobalStats") or {}
    for key in ("bytesAssigned", "bytes", "partitionsAssigned"):
        value = stats.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0


def _run_estimate(prepared: PreparedQuery) -> CostEstimate:
    sql = _prepare_sql(prepared)
    conn = _connect(prepared.warehouse_role)
    try:
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN USING JSON {sql}", _bind(prepared))
            row = cur.fetchone()
    except Exception as exc:
        raise _safe(exc) from exc
    finally:
        conn.close()
    payload = row[0] if row else None
    return CostEstimate(plan_cost=_explain_bytes(payload) if isinstance(payload, str) else 0.0)


def _run_execute(prepared: PreparedQuery) -> QueryResult:
    sql = _prepare_sql(prepared)
    conn = _connect(prepared.warehouse_role)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, _bind(prepared))
            columns = [desc[0] for desc in cur.description or []]
            rows = cur.fetchmany(prepared.row_limit)
    except Exception as exc:
        raise _safe(exc) from exc
    finally:
        conn.close()
    dict_rows: list[dict[str, Any]] = [dict(zip(columns, row, strict=False)) for row in rows]
    return QueryResult(columns=columns, rows=dict_rows, row_count=len(dict_rows))


async def estimate(prepared: PreparedQuery) -> CostEstimate:
    return await asyncio.to_thread(_run_estimate, prepared)


async def execute(prepared: PreparedQuery) -> QueryResult:
    return await asyncio.to_thread(_run_execute, prepared)
