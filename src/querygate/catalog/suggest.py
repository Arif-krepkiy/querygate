"""Near-miss suggestions for unknown table and column names."""

from __future__ import annotations

from difflib import get_close_matches
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from querygate.catalog.models import CatalogModel, SemanticCatalog

# Below this ratio the "suggestion" is noise, and a confidently wrong hint is
# worse than none: the agent will take it.
_CUTOFF = 0.6
_MAX_SUGGESTIONS = 3


def _quoted(names: list[str]) -> str:
    return " or ".join(f"'{name}'" for name in names)


def similar_models(catalog: SemanticCatalog, name: str) -> list[str]:
    """Catalog model names close to *name* in spelling."""
    by_lower = {model.name.lower(): model.name for model in catalog.models}
    matches = get_close_matches(name.lower(), list(by_lower), n=_MAX_SUGGESTIONS, cutoff=_CUTOFF)
    return [by_lower[m] for m in matches]


def models_with_column(catalog: SemanticCatalog, column: str) -> list[CatalogModel]:
    """Every model that really does have a column by this name."""
    wanted = column.lower()
    return [model for model in catalog.models if wanted in model.column_names()]


def _join_hint(source: CatalogModel, target: CatalogModel) -> str:
    """How to reach *target* from *source*, if the project declares the way."""
    for join in source.joins:
        if join.to_model.lower() == target.name.lower():
            return f" (join on {source.name}.{join.from_column} = {target.name}.{join.to_column})"
    for join in target.joins:
        if join.to_model.lower() == source.name.lower():
            return f" (join on {source.name}.{join.to_column} = {target.name}.{join.from_column})"
    return ""


def table_hint(catalog: SemanticCatalog, name: str) -> str:
    """Suffix for an unknown-table error. Empty when nothing is close enough."""
    similar = similar_models(catalog, name)
    return f" Did you mean {_quoted(similar)}?" if similar else ""


def column_hint(catalog: SemanticCatalog, model: CatalogModel, column: str) -> str:
    """Suffix for an unknown-column error.

    A misspelling on the right table is reported first: it is the likelier
    mistake and the cheaper fix. Failing that, if the column exists elsewhere,
    say where, with the join keys when the project declares them.
    """
    by_lower = {col.name.lower(): col.name for col in model.columns}
    near = get_close_matches(column.lower(), list(by_lower), n=_MAX_SUGGESTIONS, cutoff=_CUTOFF)
    if near:
        return f" Did you mean {_quoted([by_lower[m] for m in near])}?"

    elsewhere = [other for other in models_with_column(catalog, column) if other.name != model.name]
    if elsewhere:
        described = [f"'{other.name}'{_join_hint(model, other)}" for other in elsewhere[:_MAX_SUGGESTIONS]]
        return f" '{column}' exists on {' or '.join(described)}."
    return ""
