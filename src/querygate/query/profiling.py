"""Build a governed data-profiling query."""

from __future__ import annotations

from typing import TYPE_CHECKING

from querygate.query.prepare import PreparedQuery, prepare_query
from querygate.query.validation import GroundingError

if TYPE_CHECKING:
    from querygate.catalog.models import CatalogModel, SemanticCatalog

# Profiling one column costs 3-4 aggregates; a wide table would build an
# enormous statement, so cap it and tell the caller to ask for specific columns.
MAX_PROFILED_COLUMNS = 12

# min/max only make sense (and only parse everywhere) for ordered types.
_RANGE_TYPES = (
    "int",
    "float",
    "double",
    "numeric",
    "decimal",
    "real",
    "date",
    "timestamp",
    "time",
    "bigint",
    "smallint",
)


def _has_range(column_type: str) -> bool:
    lowered = (column_type or "").lower()
    return any(token in lowered for token in _RANGE_TYPES)


def select_columns(model: CatalogModel, requested: list[str] | None) -> list[str]:
    """Validate requested columns, or pick a sensible default set."""
    if requested:
        valid = model.column_names()
        unknown = [c for c in requested if c.lower() not in valid]
        if unknown:
            msg = (
                f"Column(s) not in '{model.name}': {', '.join(sorted(unknown))}. "
                f"Use qg_describe_model to see valid columns."
            )
            raise GroundingError(msg)
        if len(requested) > MAX_PROFILED_COLUMNS:
            msg = f"Profile at most {MAX_PROFILED_COLUMNS} columns at a time; narrow the list."
            raise GroundingError(msg)
        return list(requested)
    # Default: the first N columns, skipping the tenant column, whose profile is
    # meaningless once the query is scoped to that very tenant.
    names = [c.name for c in model.columns if c.name != model.tenant_column]
    return names[:MAX_PROFILED_COLUMNS]


def build_profile_query(
    model: CatalogModel,
    columns: list[str],
    catalog: SemanticCatalog,
    tenant_scopes: frozenset[str],
) -> PreparedQuery:
    """One statement returning every requested column's statistics.

    Aggregates are plain SQL (``count``/``count distinct``/``min``/``max``) so
    the same query runs on any engine. Identifiers come from the catalog, never
    from raw caller input, so the f-string carries nothing user-controlled.
    """
    types = {c.name.lower(): c.type for c in model.columns}
    parts = ["count(*) AS qg_rows"]
    for index, column in enumerate(columns):
        quoted = f'"{column}"'
        parts.append(f"count({quoted}) AS qg_{index}_nonnull")
        parts.append(f"count(DISTINCT {quoted}) AS qg_{index}_distinct")
        if _has_range(types.get(column.lower(), "")):
            parts.append(f"min({quoted}) AS qg_{index}_min")
            parts.append(f"max({quoted}) AS qg_{index}_max")

    sql = f"SELECT {', '.join(parts)} FROM {model.name}"  # noqa: S608 (catalog-validated identifiers)
    return prepare_query(sql, catalog, tenant_scopes, limit=1)


def shape_profile(row: dict, columns: list[str], model: CatalogModel) -> dict[str, object]:
    """Turn the single wide result row into per-column statistics."""
    total = int(row.get("qg_rows") or 0)
    stats: list[dict[str, object]] = []
    types = {c.name.lower(): c.type for c in model.columns}
    for index, column in enumerate(columns):
        non_null = int(row.get(f"qg_{index}_nonnull") or 0)
        entry: dict[str, object] = {
            "column": column,
            "type": types.get(column.lower(), ""),
            "non_null": non_null,
            "null_fraction": round(1 - (non_null / total), 4) if total else None,
            "distinct": int(row.get(f"qg_{index}_distinct") or 0),
        }
        if f"qg_{index}_min" in row:
            entry["min"] = row[f"qg_{index}_min"]
            entry["max"] = row[f"qg_{index}_max"]
        stats.append(entry)
    return {"row_count": total, "columns": stats}
