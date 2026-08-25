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


async def estimate(prepared: PreparedQuery) -> CostEstimate:
    """Pre-execution cost signal for the configured engine."""
    return await _adapter().estimate(prepared)


async def execute(prepared: PreparedQuery) -> QueryResult:
    """Run the governed, row-limited query on the configured engine."""
    return await _adapter().execute(prepared)


__all__ = ["CostEstimate", "QueryResult", "WarehouseError", "estimate", "execute"]
