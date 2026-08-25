"""Engine-idiom rewrites of the canonical tenant predicate.

Governance injects and re-proves exactly one shape,
``<handle>.<tenant_column> = ANY(<placeholder>)``, which is Postgres/DuckDB
syntax. BigQuery wants ``IN UNNEST(@param)``; Snowflake wants
``ARRAY_CONTAINS(<col>::VARIANT, PARSE_JSON(:param))``.

The rewrite runs after the central gate and re-proves its own output: the count
of engine-form predicates must equal the canonical ones it replaced, and no
canonical form may survive. A rewrite that dropped or mangled a predicate fails
closed here instead of executing ungoverned.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from querygate.constants import TENANT_PARAM_NAME
from querygate.warehouse.types import WarehouseError


def _is_tenant_param(node: exp.Expression) -> bool:
    """True for our tenant placeholder in either rendered form.

    ``:name`` parses to ``Placeholder`` (Postgres/DuckDB/Snowflake) and ``@name`` to
    ``Parameter`` (BigQuery), so both are accepted. The name is what identifies it,
    and that name is reserved: validation rejects agent SQL mentioning it, so it can
    only appear where the server put it.
    """
    if isinstance(node, exp.Placeholder):
        inner = node.this
        name = inner.name if isinstance(inner, exp.Expression) else str(inner or "")
    elif isinstance(node, exp.Parameter):
        name = node.name
    else:
        return False
    return name == TENANT_PARAM_NAME


def _carries_tenant_param(node: exp.Expression) -> bool:
    return any(_is_tenant_param(child) for child in node.walk())


def _canonical_predicates(tree: exp.Expression) -> list[exp.EQ]:
    """Every ``<column> = ANY(<tenant placeholder>)`` node in the tree."""
    found: list[exp.EQ] = []
    for node in tree.find_all(exp.EQ):
        rhs = node.expression
        if isinstance(rhs, exp.Any) and _carries_tenant_param(rhs) and isinstance(node.this, exp.Column):
            found.append(node)
    return found


def _parse(sql: str, dialect: str) -> exp.Expression:
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.ParseError as exc:  # pragma: no cover (pipeline parsed it already)
        msg = f"Could not parse governed SQL for the {dialect} rewrite."
        raise WarehouseError(msg) from exc
    if tree is None:  # pragma: no cover (pipeline rejects empty SQL)
        msg = "Empty SQL cannot be rewritten."
        raise WarehouseError(msg)
    return tree


def _count_engine_form(tree: exp.Expression, node_type: type[exp.Expression]) -> int:
    return sum(1 for node in tree.find_all(node_type) if _carries_tenant_param(node))


def _assert_rewritten(sql: str, dialect: str, expected: int, node_type: type[exp.Expression]) -> None:
    """Re-derive the proof from the rewritten text, sharing no state with the rewrite."""
    tree = _parse(sql, dialect)
    leftover = len(_canonical_predicates(tree))
    produced = _count_engine_form(tree, node_type)
    if leftover or produced != expected:
        msg = (
            "Tenant predicate rewrite failed its own check "
            f"(expected {expected} {node_type.__name__} predicate(s), found {produced}; "
            f"{leftover} un-rewritten). Refusing to execute."
        )
        raise WarehouseError(msg)


def _rewrite(sql: str, dialect: str, template: str, node_type: type[exp.Expression]) -> str:
    """Replace every canonical predicate with ``template`` and re-prove the result.

    ``template`` is formatted with ``column`` (the rendered left-hand side) and
    ``param`` (the tenant placeholder in this engine's syntax).
    """
    tree = _parse(sql, dialect)
    targets = _canonical_predicates(tree)
    if not targets:
        # A query over public tables only: nothing to govern, nothing to rewrite.
        return sql
    for node in targets:
        column_sql = node.this.sql(dialect=dialect)
        fragment = template.format(column=column_sql, param=TENANT_PARAM_NAME)
        node.replace(sqlglot.condition(fragment, dialect=dialect))
    rewritten = tree.sql(dialect=dialect)
    _assert_rewritten(rewritten, dialect, len(targets), node_type)
    return rewritten


def to_in_unnest(sql: str, dialect: str = "bigquery") -> str:
    """BigQuery: ``col = ANY(@p)`` becomes ``col IN UNNEST(@p)``."""
    return _rewrite(sql, dialect, "{column} IN UNNEST(@{param})", exp.In)


def to_array_contains(sql: str, dialect: str = "snowflake") -> str:
    """Snowflake: ``col = ANY(:p)`` becomes ``ARRAY_CONTAINS(col::VARIANT, PARSE_JSON(:p))``.

    The scope list binds as one JSON string rather than an expanded list of
    literals: a single scalar bind has unambiguous semantics across driver
    versions, and tenant values still never touch the SQL text.
    """
    return _rewrite(
        sql,
        dialect,
        "ARRAY_CONTAINS({column}::VARIANT, PARSE_JSON(:{param}))",
        exp.ArrayContains,
    )


def to_pyformat(sql: str, names: tuple[str, ...]) -> str:
    """Render ``:name`` placeholders as ``%(name)s`` for pyformat drivers.

    Only the server's own reserved parameter names are substituted. Those names
    are rejected in agent SQL by validation, so they cannot appear anywhere the
    server did not put them, the same invariant ``PreparedQuery.bind_params``
    relies on.
    """
    for name in names:
        sql = sql.replace(f":{name}", f"%({name})s")
    return sql
