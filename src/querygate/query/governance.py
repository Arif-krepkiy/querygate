"""Tenant governance: inject the row-security filter, then assert it independently.

Every SELECT scope reading a governed table gets
``<handle>.<tenant_column> = ANY(:qg_tenant_scopes)`` AND-ed into its WHERE.
The values are bound by the driver, never interpolated.

``assert_tenant_filter_present`` re-parses the final SQL and re-derives the
proof from the text. It shares no state with the injector, so a bug in the
injector does not also pass the gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from querygate import config
from querygate.constants import TENANT_PARAM_NAME
from querygate.query.validation import collect_cte_names, scope_tables, table_handle, table_ref

if TYPE_CHECKING:
    from querygate.catalog.models import CatalogModel, SemanticCatalog


class GovernanceError(ValueError):
    """Raised when a query cannot be governed or fails the governance gate."""


def _parse(sql: str) -> exp.Expression:
    try:
        parsed = sqlglot.parse_one(sql, dialect=config.SQL_DIALECT)
    except sqlglot.errors.ParseError as exc:
        msg = f"Could not parse SQL for governance: {exc}."
        raise GovernanceError(msg) from exc
    if parsed is None:
        msg = "Empty SQL cannot be governed."
        raise GovernanceError(msg)
    return parsed


def _governed_refs(
    select: exp.Select,
    catalog: SemanticCatalog,
    cte_names: frozenset[str],
) -> list[tuple[str, CatalogModel]]:
    refs: list[tuple[str, CatalogModel]] = []
    for table in scope_tables(select, cte_names):
        model = catalog.resolve_table(table_ref(table))
        if model is not None and model.governed and model.tenant_column:
            refs.append((table_handle(table), model))
    return refs


def _tenant_predicate(handle: str, tenant_column: str, param_name: str) -> exp.Expression:
    fragment = f"{handle}.{tenant_column} = ANY(:{param_name})"
    return sqlglot.condition(fragment, dialect=config.SQL_DIALECT)


def inject_tenant_filter(sql: str, catalog: SemanticCatalog, param_name: str = TENANT_PARAM_NAME) -> str:
    """Return *sql* with the tenant filter injected into every governed scope."""
    tree = _parse(sql)
    cte_names = collect_cte_names(tree)
    for select in tree.find_all(exp.Select):
        for handle, model in _governed_refs(select, catalog, cte_names):
            predicate = _tenant_predicate(handle, model.tenant_column or "", param_name)
            select.where(predicate, append=True, copy=False, dialect=config.SQL_DIALECT)
    return tree.sql(dialect=config.SQL_DIALECT)


def _and_conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    """Flatten the top-level AND chain; anything under an OR stays whole, so an
    OR-ed tautology can never satisfy the gate."""
    if node is None:
        return []
    if isinstance(node, exp.And):
        return _and_conjuncts(node.args.get("this")) + _and_conjuncts(node.args.get("expression"))
    return [node]


def _placeholder_name(node: exp.Expression) -> str:
    inner = node.this
    if isinstance(inner, exp.Identifier):
        return inner.name
    return inner if isinstance(inner, str) else getattr(inner, "name", "")


def _matches_tenant_predicate(node: exp.Expression, handle: str, tenant_column: str) -> bool:
    """Exactly ``<handle>.<tenant_column> = ANY(<our placeholder>)``. Anything
    else (IS NULL, a different parameter, a bare mention) does not count."""
    if not isinstance(node, exp.EQ):
        return False
    any_node = node.expression if isinstance(node.expression, exp.Any) else None
    if any_node is None:
        return False
    placeholder = any_node.find(exp.Placeholder)
    if placeholder is None or _placeholder_name(placeholder) != TENANT_PARAM_NAME:
        return False
    column = node.this if isinstance(node.this, exp.Column) else None
    return (
        column is not None
        and column.name.lower() == tenant_column.lower()
        and (not column.table or column.table.lower() == handle.lower())
    )


def _scope_has_tenant_filter(select: exp.Select, handle: str, tenant_column: str) -> bool:
    where = select.args.get("where")
    if where is None:
        return False
    return any(_matches_tenant_predicate(c, handle, tenant_column) for c in _and_conjuncts(where.this))


def assert_tenant_filter_present(sql: str, catalog: SemanticCatalog) -> None:
    """Independent gate: re-parse and verify every governed scope is filtered.

    Raises GovernanceError naming the offending table otherwise. Runs before
    every execution. Injection feeds it SQL that passes by construction; this
    catches anything that slipped through.
    """
    tree = _parse(sql)
    cte_names = collect_cte_names(tree)
    for select in tree.find_all(exp.Select):
        for handle, model in _governed_refs(select, catalog, cte_names):
            if not _scope_has_tenant_filter(select, handle, model.tenant_column or ""):
                msg = (
                    f"Governance violation: query reads governed table '{model.name}' without a "
                    f"restrictive '{model.tenant_column}' filter. Rejected before execution."
                )
                raise GovernanceError(msg)
