"""Shared warehouse result types and the error every adapter raises."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class WarehouseError(Exception):
    """Raised when a warehouse operation fails in a user-facing, safe way."""


@dataclass(frozen=True)
class CostEstimate:
    """Pre-execution cost signal. The unit is engine-specific (Postgres planner
    cost units, DuckDB estimated rows, BigQuery bytes), so it is only ever
    compared against that engine's configured ceiling."""

    plan_cost: float


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    plan_cost: float | None = None
