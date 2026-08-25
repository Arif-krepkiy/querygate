"""Column-level security: mask PII columns in the AST.

Masked columns are confined to the SELECT list. Allowing one in a predicate
leaves an oracle: repeated equality tests read the column a value at a time
while every returned value stays masked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from querygate import config

if TYPE_CHECKING:
    from querygate.catalog.models import CatalogModel, SemanticCatalog


class MaskingError(ValueError):
    """Raised when a masked column is used somewhere it cannot be masked."""


# Where a masked column may NOT appear: anything that lets the caller probe the
# underlying value rather than merely display it.
_PREDICATE_CLAUSES = ("where", "join", "joins", "group", "order", "having", "qualify")


def _mask_expression(column: exp.Column, policy: str) -> exp.Expression:
    """The replacement expression, aliased back to the original column name.

    ``md5`` is used rather than ``sha256`` because it exists in every dialect
    this project targets. It is a pseudonymiser, not a security hash: the threat
    model is "an analyst must not read the value", not "an attacker must not
    brute-force it offline". A deployment handling regulated data should
    swap in a keyed HMAC with a secret the query layer never returns.
    """
    name = column.name
    if policy == "redact":
        replacement: exp.Expression = exp.Literal.string("***")
    else:  # "hash"
        replacement = exp.func("md5", exp.cast(column.copy(), "TEXT"))
    return exp.alias_(replacement, name)


def _resolve(handles: dict[str, CatalogModel], column: exp.Column) -> CatalogModel | None:
    """Which model a column belongs to, by its qualifier or unambiguously."""
    qualifier = column.table.lower()
    if qualifier:
        return handles.get(qualifier)
    # Unqualified: only safe to attribute when exactly one model is in play.
    models = list(handles.values())
    return models[0] if len(models) == 1 else None


def _masked_policy(handles: dict[str, CatalogModel], column: exp.Column) -> str | None:
    model = _resolve(handles, column)
    if model is None:
        return None
    return model.masked_columns().get(column.name.lower())


def _iter_nodes(node: object) -> list[exp.Expression]:
    """Normalise a clause arg, which sqlglot stores as a node or a list."""
    if isinstance(node, list):
        return [n for n in node if isinstance(n, exp.Expression)]
    return [node] if isinstance(node, exp.Expression) else []


def _assert_not_in_predicates(tree: exp.Expression, handles: dict[str, CatalogModel]) -> None:
    """Reject a masked column anywhere it could be used to probe values."""
    offenders: set[str] = set()
    for select in tree.find_all(exp.Select):
        for clause in _PREDICATE_CLAUSES:
            for node in _iter_nodes(select.args.get(clause)):
                for column in node.find_all(exp.Column):
                    if _masked_policy(handles, column):
                        offenders.add(column.name)
    if offenders:
        names = ", ".join(sorted(offenders))
        msg = (
            f"Column(s) {names} are masked and can only be selected, not used in a "
            f"filter, join, or grouping. Filtering on a masked column would let its value be guessed one "
            f"query at a time. Aggregate over it, or use a column you may read."
        )
        raise MaskingError(msg)


def _scope_models(select: exp.Select, handles: dict[str, CatalogModel]) -> list[tuple[str, CatalogModel]]:
    """The (handle, model) pairs this particular SELECT reads directly."""
    from querygate.query.validation import (
        collect_cte_names,
        scope_tables,
        table_handle,
    )

    pairs: list[tuple[str, CatalogModel]] = []
    for table in scope_tables(select, collect_cte_names(select)):
        handle = table_handle(table)
        model = handles.get(handle.lower())
        if model is not None:
            pairs.append((handle, model))
    return pairs


def _expand_stars(select: exp.Select, handles: dict[str, CatalogModel]) -> None:
    """Rewrite ``SELECT *`` into explicit columns when masking is in play.

    Without this, the laziest possible query defeats column-level security
    entirely: ``*`` is a Star node, not a Column, so a rewrite that walks
    columns never sees the masked one. Expanding first means every projection
    is a real column reference that the masking pass can act on.

    Only expanded when a table in scope actually has a masked column, so
    ordinary queries keep their ``*`` and stay readable.
    """
    scoped = _scope_models(select, handles)
    if not any(model.masked_columns() for _, model in scoped):
        return

    replacements: list[exp.Expression] = []
    changed = False
    for projection in select.expressions:
        star_owner: str | None = None
        if isinstance(projection, exp.Star):
            star_owner = "*"
        elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            star_owner = projection.table

        if star_owner is None:
            replacements.append(projection)
            continue

        changed = True
        for handle, model in scoped:
            if star_owner not in ("*", handle):
                continue
            replacements.extend(
                exp.column(col.name, table=handle if len(scoped) > 1 else None) for col in model.columns
            )
    if changed:
        select.set("expressions", replacements)


def apply_masking(sql: str, catalog: SemanticCatalog, handles: dict[str, CatalogModel]) -> str:
    """Rewrite masked columns in the projection; reject them in predicates."""
    if not config.MASKING_ENABLED:
        return sql
    tree = sqlglot.parse_one(sql, dialect=config.SQL_DIALECT)
    _assert_not_in_predicates(tree, handles)

    for select in tree.find_all(exp.Select):
        _expand_stars(select, handles)
        for projection in list(select.expressions):
            # A bare masked column is replaced by an *aliased* expression, so the
            # result keeps the caller's column name and the agent's downstream
            # reasoning about the shape of the result still holds.
            if isinstance(projection, exp.Column):
                policy = _masked_policy(handles, projection)
                if policy:
                    projection.replace(_mask_expression(projection, policy))
                continue
            # Otherwise the column sits inside a larger expression or an
            # explicit alias, both of which already carry a name.
            for column in list(projection.find_all(exp.Column)):
                policy = _masked_policy(handles, column)
                if policy:
                    column.replace(_mask_expression(column, policy).this)
    return tree.sql(dialect=config.SQL_DIALECT)


def assert_masking_applied(sql: str, catalog: SemanticCatalog, handles: dict[str, CatalogModel]) -> None:
    """Independent gate: no masked column may survive as a bare reference.

    Mirrors the governance assert: re-derived from the final SQL rather than
    trusting that the rewrite above ran.
    """
    if not config.MASKING_ENABLED:
        return
    tree = sqlglot.parse_one(sql, dialect=config.SQL_DIALECT)
    _assert_not_in_predicates(tree, handles)
    for select in tree.find_all(exp.Select):
        # A surviving star over a table with masked columns would return them
        # untouched; the expansion above should have removed it.
        if any(model.masked_columns() for _, model in _scope_models(select, handles)):
            for projection in select.expressions:
                if isinstance(projection, exp.Star) or (
                    isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
                ):
                    msg = (
                        "Masking violation: a wildcard projection over a table with masked "
                        "columns was not expanded. Rejected before execution."
                    )
                    raise MaskingError(msg)
        for projection in select.expressions:
            target = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(target, exp.Column) and _masked_policy(handles, target):
                msg = (
                    f"Masking violation: column '{target.name}' would be returned unmasked. "
                    f"Rejected before execution."
                )
                raise MaskingError(msg)
