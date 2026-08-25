"""BigQuery adapter. Dry-run to estimate bytes, then execute read-only."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from querygate import config
from querygate.constants import TENANT_PARAM_NAME
from querygate.log_setup import get_logger
from querygate.warehouse.tenant_sql import to_in_unnest
from querygate.warehouse.types import CostEstimate, QueryResult, WarehouseError

if TYPE_CHECKING:
    from google.cloud import bigquery as bq

    from querygate.query.prepare import PreparedQuery

_log = get_logger("querygate.warehouse.bigquery")

_REJECTED = "The warehouse rejected the query. Check table/column names and types, then retry."

_client: bq.Client | None = None


def _connect() -> bq.Client:
    """One process-wide client; the BigQuery client is thread-safe."""
    global _client
    if _client is None:
        from google.cloud import bigquery

        _client = bigquery.Client(project=config.BQ_PROJECT or None, location=config.BQ_LOCATION or None)
        _log.info("BigQuery client ready: project=%s location=%s", _client.project, config.BQ_LOCATION)
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests."""
    global _client
    _client = None


def _query_parameters(prepared: PreparedQuery) -> list[Any]:
    """Bind the tenant scopes as an ARRAY<STRING> and any scalars as strings.

    Values are always bound, never interpolated. Only parameters the SQL
    actually uses are sent. BigQuery rejects a query carrying an undeclared
    parameter, so ``bind_params`` filtering is what keeps one ``PreparedQuery``
    valid across engines.
    """
    from google.cloud import bigquery

    params: list[Any] = []
    for name, value in prepared.bind_params().items():
        if name == TENANT_PARAM_NAME:
            params.append(bigquery.ArrayQueryParameter(name, "STRING", list(value)))  # type: ignore[arg-type]
        else:
            params.append(bigquery.ScalarQueryParameter(name, "STRING", value))
    return params


def _job_config(prepared: PreparedQuery, *, dry_run: bool) -> bq.QueryJobConfig:
    from google.cloud import bigquery

    max_bytes = config.BQ_MAX_BYTES_BILLED or None
    return bigquery.QueryJobConfig(
        dry_run=dry_run,
        use_query_cache=not dry_run,
        use_legacy_sql=False,
        query_parameters=_query_parameters(prepared),
        # Only meaningful on the real run; a dry run bills nothing.
        maximum_bytes_billed=None if dry_run else max_bytes,
    )


def _safe(exc: Exception) -> WarehouseError:
    """Map a client error to an actionable message that leaks no internals."""
    from google.api_core import exceptions as gexc

    if isinstance(exc, gexc.Forbidden):
        return WarehouseError("Access denied by the warehouse for one of the requested tables.")
    if isinstance(exc, gexc.NotFound):
        return WarehouseError("A referenced table or dataset was not found. It may have been renamed.")
    if isinstance(exc, gexc.DeadlineExceeded | TimeoutError):
        return WarehouseError(
            "The query took too long and was cancelled. Add a filter (often a date or partition) "
            "or select fewer columns."
        )
    if isinstance(exc, gexc.BadRequest):
        message = str(exc)
        if "maximum_bytes_billed" in message or "exceeded limit" in message:
            return WarehouseError(
                "The query would scan more data than this server allows. Narrow the date range "
                "or partition filter and retry."
            )
        # Syntax / type errors can echo values from the data. Log server-side only.
        _log.warning("bigquery bad request: %s", message[:300])
        return WarehouseError(_REJECTED)
    _log.warning("bigquery error: %s: %s", type(exc).__name__, str(exc)[:300])
    return WarehouseError(_REJECTED)


def _run_estimate(prepared: PreparedQuery) -> CostEstimate:
    sql = to_in_unnest(prepared.sql, dialect=config.SQL_DIALECT)
    try:
        job = _connect().query(sql, job_config=_job_config(prepared, dry_run=True))
    except Exception as exc:
        raise _safe(exc) from exc
    # A dry run completes client-side: bytes are known without executing.
    return CostEstimate(plan_cost=float(job.total_bytes_processed or 0))


def _run_execute(prepared: PreparedQuery) -> QueryResult:
    sql = to_in_unnest(prepared.sql, dialect=config.SQL_DIALECT)
    timeout_s = config.STATEMENT_TIMEOUT_MS / 1000
    try:
        job = _connect().query(sql, job_config=_job_config(prepared, dry_run=False))
        iterator = job.result(max_results=prepared.row_limit, timeout=timeout_s)
        columns = [field.name for field in iterator.schema]
        rows = [dict(row.items()) for row in iterator]
    except Exception as exc:
        raise _safe(exc) from exc
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        plan_cost=float(job.total_bytes_processed or 0),
    )


async def estimate(prepared: PreparedQuery) -> CostEstimate:
    return await asyncio.to_thread(_run_estimate, prepared)


async def execute(prepared: PreparedQuery) -> QueryResult:
    return await asyncio.to_thread(_run_execute, prepared)
