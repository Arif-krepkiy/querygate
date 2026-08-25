"""Warehouse facade. The adapter is picked by QG_WAREHOUSE; nothing upstream
knows which engine answers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from querygate import config
from querygate.warehouse.types import CostEstimate, QueryResult, WarehouseError

if TYPE_CHECKING:
    from querygate.query.prepare import PreparedQuery

# engine name -> module implementing estimate() / execute()
_ADAPTERS = {
    "postgres": "querygate.warehouse.postgres",
    "duckdb": "querygate.warehouse.duckdb_backend",
    "bigquery": "querygate.warehouse.bigquery",
    "snowflake": "querygate.warehouse.snowflake",
}


def _adapter():
    from importlib import import_module

    module_path = _ADAPTERS.get(config.WAREHOUSE)
    if module_path is None:
        supported = ", ".join(sorted(_ADAPTERS))
        msg = f"Unknown QG_WAREHOUSE '{config.WAREHOUSE}'. Supported: {supported}."
        raise WarehouseError(msg)
    try:
        return import_module(module_path)
    except ImportError as exc:
        msg = (
            f"The '{config.WAREHOUSE}' adapter needs its driver: "
            f"install querygate[{config.WAREHOUSE}] ({exc})."
        )
        raise WarehouseError(msg) from exc


def _with_identity(prepared: PreparedQuery) -> PreparedQuery:
    """Stamp the caller's warehouse role on the query, in warehouse mode.

    Done here rather than in the tools because every path to an engine (ad-hoc
    SQL, metrics, profiling, filter values) funnels through estimate/execute, so
    none of them can reach a warehouse without the caller's identity.
    """
    from dataclasses import replace

    from querygate.auth.context import current_principal
    from querygate.query.identity import resolve_warehouse_role

    role = resolve_warehouse_role(current_principal())
    return prepared if role is None else replace(prepared, warehouse_role=role)


async def estimate(prepared: PreparedQuery) -> CostEstimate:
    """Pre-execution cost signal for the configured engine."""
    return await _adapter().estimate(_with_identity(prepared))


async def execute(prepared: PreparedQuery) -> QueryResult:
    """Run the governed, row-limited query on the configured engine."""
    return await _adapter().execute(_with_identity(prepared))


__all__ = ["CostEstimate", "QueryResult", "WarehouseError", "estimate", "execute"]
