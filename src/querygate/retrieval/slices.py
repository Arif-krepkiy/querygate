from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from querygate.catalog.models import CatalogModel, Join, Metric


def model_summary(model: CatalogModel) -> dict[str, object]:
    """Search-result shape: enough to pick a model, nothing more."""
    return {
        "name": model.name,
        "domain": model.domain,
        "description": model.description,
        "governed": model.governed,
    }


def metric_summary(metric: Metric) -> dict[str, object]:
    """Authoritative metric definition the agent should compute, not reinvent."""
    return {
        "name": metric.name,
        "label": metric.label,
        "description": metric.description,
        "model": metric.model,
        "expr": metric.expr,
        "filter": metric.filter,
        "certified": metric.certified,
    }


def join_summary(join: Join) -> dict[str, object]:
    """A declared join key, so the agent stops guessing how tables relate."""
    return {"to_model": join.to_model, "on": f"{join.from_column} = {join.to_column}"}


def model_detail(model: CatalogModel, metrics: tuple[Metric, ...] = ()) -> dict[str, object]:
    """describe_model shape: full columns + any metrics defined on this model."""
    detail: dict[str, object] = {
        "name": model.name,
        "domain": model.domain,
        "description": model.description,
        "governed": model.governed,
        "columns": [{"name": c.name, "type": c.type, "description": c.description} for c in model.columns],
    }
    if model.partition_column:
        # Told to the agent unconditionally, even where a missing filter is not
        # refused: knowing which column prunes is how it writes a cheap query.
        detail["partition_column"] = model.partition_column
    if model.joins:
        detail["joins"] = [join_summary(j) for j in model.joins]
    if metrics:
        detail["metrics"] = [metric_summary(m) for m in metrics]
    return detail
