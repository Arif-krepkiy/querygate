"""The warehouse adapter contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from querygate.query.prepare import PreparedQuery
    from querygate.warehouse.types import CostEstimate, QueryResult


class WarehouseAdapter(Protocol):
    async def estimate(self, prepared: PreparedQuery) -> CostEstimate: ...
    async def execute(self, prepared: PreparedQuery) -> QueryResult: ...
