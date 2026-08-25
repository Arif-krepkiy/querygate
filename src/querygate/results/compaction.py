"""Columnar result shape and the truncation flag."""

from __future__ import annotations

import datetime
import decimal
from typing import Any


def _json_safe(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.date | datetime.datetime | datetime.time):
        return value.isoformat()
    if isinstance(value, bytes | memoryview):
        return bytes(value).hex()
    return value


def compact_result(
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    row_limit: int,
) -> dict[str, object]:
    matrix = [[_json_safe(row.get(col)) for col in columns] for row in rows]
    return {
        "columns": columns,
        "rows": matrix,
        "row_count": len(rows),
        "truncated": len(rows) >= row_limit,
    }
