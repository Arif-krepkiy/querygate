"""Compile a metric definition to SQL."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from querygate.query.prepare import PreparedQuery, prepare_query
from querygate.query.validation import GroundingError

if TYPE_CHECKING:
    from querygate.catalog.models import Metric, SemanticCatalog

_DATE_TYPES = ("date", "timestamp", "time")


def _validate_date(value: str, label: str) -> str:
    """Prove a value is a date before it is allowed near SQL.

    Parsing with ``fromisoformat`` is the check *and* the sanitiser: anything
    that survives it is a real date, so embedding it as a literal cannot carry
    injection. That is why this does not need a bound parameter.
    """
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        msg = f"{label} must be an ISO date like 2024-01-31, got '{value}'."
        raise GroundingError(msg) from exc


def resolve_metric(catalog: SemanticCatalog, name: str) -> Metric:
    metric = catalog.get_metric(name)
    if metric is None:
        available = ", ".join(sorted(m.name for m in catalog.metrics)) or "none defined"
        msg = f"Unknown metric '{name}'. Defined metrics: {available}."
        raise GroundingError(msg)
    return metric


def build_metric_query(
    catalog: SemanticCatalog,
    metric: Metric,
    tenant_scopes: frozenset[str],
    *,
    dimensions: list[str] | None = None,
    time_column: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> PreparedQuery:
    """Assemble ``SELECT <dims>, <expr> FROM <model> WHERE <filter> GROUP BY <dims>``."""
    model = catalog.get_model(metric.model)
    if model is None:
        msg = f"Metric '{metric.name}' references unknown model '{metric.model}'."
        raise GroundingError(msg)

    valid = model.column_names()
    masked = model.masked_columns()
    requested = list(dimensions or [])

    unknown = [d for d in requested if d.lower() not in valid]
    if unknown:
        msg = (
            f"Dimension(s) not on '{model.name}': {', '.join(sorted(unknown))}. "
            f"Use qg_describe_model to see valid columns."
        )
        raise GroundingError(msg)
    # Grouping by a masked column is refused upstream anyway; saying so here
    # gives the agent a better error than a generic masking rejection.
    blocked = [d for d in requested if d.lower() in masked]
    if blocked:
        msg = f"Cannot group by masked column(s): {', '.join(sorted(blocked))}."
        raise GroundingError(msg)

    select_parts = [f'"{d}"' for d in requested]
    select_parts.append(f"{metric.expr} AS {metric.name}")
    sql = f"SELECT {', '.join(select_parts)} FROM {model.name}"  # noqa: S608 (catalog-validated)

    conditions = [metric.filter] if metric.filter else []
    if start or end:
        if not time_column:
            msg = "A time range needs time_column: the column to filter on."
            raise GroundingError(msg)
        if time_column.lower() not in valid:
            msg = f"Unknown time_column '{time_column}' on '{model.name}'."
            raise GroundingError(msg)
        column_type = next((c.type for c in model.columns if c.name.lower() == time_column.lower()), "")
        if not any(token in column_type.lower() for token in _DATE_TYPES):
            msg = f"time_column '{time_column}' is {column_type or 'untyped'}, not a date or timestamp."
            raise GroundingError(msg)
        if start:
            conditions.append(f"\"{time_column}\" >= DATE '{_validate_date(start, 'start')}'")
        if end:
            conditions.append(f"\"{time_column}\" <= DATE '{_validate_date(end, 'end')}'")

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    if requested:
        group_by = ", ".join(f'"{d}"' for d in requested)
        sql += f" GROUP BY {group_by} ORDER BY {group_by}"

    return prepare_query(sql, catalog, tenant_scopes, limit=limit)
