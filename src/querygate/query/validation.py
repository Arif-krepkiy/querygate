"""Grounded, read-only validation of candidate SQL. Pure, no I/O."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from querygate import config
from querygate.catalog.suggest import column_hint, table_hint
from querygate.constants import RESERVED_TOKENS

if TYPE_CHECKING:
    from querygate.catalog.models import CatalogModel, SemanticCatalog


class GroundingError(ValueError):
    """Raised when candidate SQL is not a grounded, read-only SELECT."""


_ALLOWED_TOP = (exp.Select, exp.Union, exp.Except, exp.Intersect)
_FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.TruncateTable,
)


def collect_cte_names(tree: exp.Expression) -> frozenset[str]:
    """Bare names bound as CTEs anywhere in the statement."""
    return frozenset(cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE) if cte.alias_or_name)


def is_cte_reference(table: exp.Table, cte_names: frozenset[str]) -> bool:
    """A CTE is referenced by bare name only; a schema-qualified reference is a
    real table even if a same-named CTE exists. Otherwise a governed physical
    table could hide behind a CTE name."""
    return not table.catalog and not table.db and table.name.lower() in cte_names


def scope_tables(select: exp.Select, cte_names: frozenset[str] = frozenset()) -> list[exp.Table]:
    """Physical tables read directly by this SELECT (FROM + JOINs).

    Subqueries are their own ``exp.Select`` nodes, handled when the walk
    reaches them.
    """
    candidates: list[exp.Table] = []
    from_ = select.args.get("from") or select.args.get("from_")
    if from_ is not None and isinstance(from_.this, exp.Table):
        candidates.append(from_.this)
    candidates.extend(
        join.this for join in select.args.get("joins") or [] if isinstance(join.this, exp.Table)
    )
    return [table for table in candidates if not is_cte_reference(table, cte_names)]


def table_ref(table: exp.Table) -> str:
    parts = [part for part in (table.catalog, table.db, table.name) if part]
    return ".".join(parts)


def table_handle(table: exp.Table) -> str:
    """Name that qualifies columns for this table: its alias, else its name."""
    return table.alias_or_name


def parse_single(sql: str, *, check_reserved: bool = True) -> exp.Expression:
    """Parse exactly one statement or raise a GroundingError.

    ``check_reserved`` guards agent-supplied SQL against our reserved
    placeholder names; server-built SQL (get_filter_values) legitimately
    contains ``:qg_search`` and passes ``check_reserved=False``.
    """
    if not sql or not sql.strip():
        msg = "SQL is empty. Provide a single read-only SELECT statement."
        raise GroundingError(msg)
    if check_reserved:
        lowered = sql.lower()
        for token in RESERVED_TOKENS:
            if token in lowered:
                msg = f"SQL contains the reserved identifier '{token}'. Rename it and retry."
                raise GroundingError(msg)
    try:
        statements = sqlglot.parse(sql, dialect=config.SQL_DIALECT)
    except sqlglot.errors.ParseError as exc:
        msg = f"SQL could not be parsed: {exc}."
        raise GroundingError(msg) from exc
    real = [stmt for stmt in statements if stmt is not None]
    if len(real) != 1:
        msg = f"Expected exactly one statement, found {len(real)}. Submit a single SELECT (no semicolons)."
        raise GroundingError(msg)
    return real[0]


def _assert_read_only(top: exp.Expression) -> None:
    if not isinstance(top, _ALLOWED_TOP):
        msg = (
            f"Only read-only SELECT queries are allowed; got a "
            f"'{type(top).__name__.upper()}' statement. Rewrite as a SELECT."
        )
        raise GroundingError(msg)
    if top.find(*_FORBIDDEN) is not None:
        msg = "Query contains a non-read-only operation (INSERT/UPDATE/DELETE/DDL). Only SELECT is permitted."
        raise GroundingError(msg)


def _resolve_handles(top: exp.Expression, catalog: SemanticCatalog) -> dict[str, CatalogModel]:
    handles: dict[str, CatalogModel] = {}
    unknown: list[str] = []
    forbidden: list[str] = []
    cte_names = collect_cte_names(top)
    for select in top.find_all(exp.Select):
        for table in scope_tables(select, cte_names):
            ref = table_ref(table)
            model = catalog.resolve_table(ref)
            if model is None:
                unknown.append(ref or table.name)
            elif config.ALLOWED_SCHEMAS and model.schema not in config.ALLOWED_SCHEMAS:
                forbidden.append(model.relation())
            else:
                handles[table_handle(table).lower()] = model
    if unknown:
        names = sorted(set(unknown))
        hints = "".join(table_hint(catalog, name) for name in names)
        msg = (
            f"Query references unknown table(s): {', '.join(names)}.{hints} "
            f"Use qg_search_catalog to find valid model names; write SQL only against catalog models."
        )
        raise GroundingError(msg)
    if forbidden:
        msg = (
            f"Query references table(s) outside the allowed schemas "
            f"({', '.join(config.ALLOWED_SCHEMAS)}): {', '.join(sorted(set(forbidden)))}."
        )
        raise GroundingError(msg)
    return handles


def _assert_columns_grounded(
    top: exp.Expression, handles: dict[str, CatalogModel], catalog: SemanticCatalog
) -> None:
    """Every column the agent tied to a known table handle must exist on it.

    Unqualified columns (CTE outputs, aliases, function results) are skipped;
    invented names show up on qualified references.
    """
    bad: dict[str, tuple[CatalogModel, str]] = {}
    for column in top.find_all(exp.Column):
        handle = column.table.lower()
        if not handle or handle not in handles:
            continue
        if column.name.lower() not in handles[handle].column_names():
            bad[f"{column.table}.{column.name}"] = (handles[handle], column.name)
    if bad:
        hints = "".join(column_hint(catalog, model, name) for model, name in bad.values())
        msg = (
            f"Query references column(s) not in the catalog: {', '.join(sorted(bad))}.{hints} "
            f"Use qg_describe_model to see valid columns."
        )
        raise GroundingError(msg)


def validate_grounded(
    sql: str, catalog: SemanticCatalog, *, check_reserved: bool = True
) -> dict[str, CatalogModel]:
    """Full check; returns the resolved handle → model map for reuse."""
    top = parse_single(sql, check_reserved=check_reserved)
    _assert_read_only(top)
    handles = _resolve_handles(top, catalog)
    _assert_columns_grounded(top, handles, catalog)
    return handles
