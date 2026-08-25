"""Static AST checks that run before the engine plans the query."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from querygate import config
from querygate.query.validation import (
    GroundingError,
    collect_cte_names,
    parse_single,
    scope_tables,
    table_handle,
)

if TYPE_CHECKING:
    from querygate.catalog.models import CatalogModel

# Predicates that actually narrow a partitioned scan. `IS NOT NULL` mentions the
# column without pruning anything, which is exactly the shape a model reaches
# for when it has been told "filter on the partition column" and has no idea
# what range to ask for.
_PRUNING_PREDICATES = (exp.EQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between, exp.In)


def assert_guardrails(sql: str, handles: dict[str, CatalogModel], *, check_reserved: bool) -> None:
    """Run every static check over agent-supplied SQL.

    Re-parses rather than threading a tree through the pipeline, matching how
    ``governance`` re-parses to assert: each pass proves its property about the
    text it was handed, so a bug in an earlier stage cannot vouch for a later
    one. Parsing is microseconds against a query about to touch a warehouse.
    """
    top = parse_single(sql, check_reserved=check_reserved)
    assert_no_cartesian_product(top, handles)
    assert_partition_filters(top, handles)


def _scope_models(select: exp.Select, handles: dict[str, CatalogModel]) -> list[tuple[str, CatalogModel]]:
    """(handle, model) for each catalog table this SELECT reads directly."""
    pairs = []
    for table in scope_tables(select, collect_cte_names(select)):
        handle = table_handle(table)
        model = handles.get(handle.lower())
        if model is not None:
            pairs.append((handle, model))
    return pairs


def _join_is_constrained(join: exp.Join, handle: str) -> bool:
    """Whether this join actually relates the joined table to the rest of the query.

    ``USING`` always does. An ``ON`` is trusted unless it can be *proven* not to
    reference the joined table: ``ON 1 = 1`` binds nothing, and
    ``ON a.x = a.y`` names only the other side. Unqualified column names are
    given the benefit of the doubt: working out which table a bare name belongs
    to is the resolver's job, not this check's.
    """
    if join.args.get("using"):
        return True
    on = join.args.get("on")
    if on is None:
        return False
    columns = list(on.find_all(exp.Column))
    if not columns:
        return False
    qualified = [col for col in columns if col.table]
    if not qualified:
        return True
    return any(col.table.lower() == handle.lower() for col in qualified)


def assert_no_cartesian_product(top: exp.Expression, handles: dict[str, CatalogModel]) -> None:
    """Reject a join between catalog tables that carries no real join condition.

    ``FROM a, b``, ``CROSS JOIN`` and ``JOIN b ON 1 = 1`` all mean the same
    thing: every row of one table paired with every row of the other. Between
    two real tables that is nearly always a forgotten condition, and it is the
    one mistake where a valid plan and a catastrophic query look identical to
    the optimiser: 100k by 100k rows plans instantly and then runs until the
    statement timeout kills it.
    """
    for select in top.find_all(exp.Select):
        scoped = {handle.lower() for handle, _ in _scope_models(select, handles)}
        if len(scoped) < 2:
            continue  # a cross join against a literal, a function or a CTE is the caller's business
        for join in select.args.get("joins") or []:
            if not isinstance(join.this, exp.Table):
                continue
            handle = table_handle(join.this)
            if handle.lower() not in scoped or _join_is_constrained(join, handle):
                continue
            msg = (
                f"Join on '{handle}' has no condition relating it to the other tables, so the "
                f"result is every combination of rows from both. Add the join keys; "
                f"qg_describe_model lists the ones this project declares."
            )
            raise GroundingError(msg)


def _has_pruning_predicate(select: exp.Select, handle: str, column: str) -> bool:
    """Whether this scope's WHERE narrows *column* rather than merely mentioning it."""
    where = select.args.get("where")
    if where is None:
        return False
    for node in where.find_all(*_PRUNING_PREDICATES):
        for ref in node.find_all(exp.Column):
            same_column = ref.name.lower() == column.lower()
            same_table = not ref.table or ref.table.lower() == handle.lower()
            if same_column and same_table:
                return True
    return False


def assert_partition_filters(top: exp.Expression, handles: dict[str, CatalogModel]) -> None:
    """Require a narrowing filter on the partition column of partitioned tables.

    On BigQuery, Snowflake or an Iceberg lakehouse this is the difference
    between reading one day and reading five years. The plan is valid either
    way, so a cost ceiling catches it only after the engine has already decided
    to scan everything, and on BigQuery the decision is the bill.

    Off by default: Postgres and DuckDB, the two adapters shipped here, care far
    less, and a check that fires on a local demo teaches people to switch it off.
    Turn it on where partitions cost money.
    """
    if not config.REQUIRE_PARTITION_FILTER:
        return
    for select in top.find_all(exp.Select):
        for handle, model in _scope_models(select, handles):
            column = model.partition_column
            if not column or _has_pruning_predicate(select, handle, column):
                continue
            msg = (
                f"'{model.name}' is partitioned by '{column}'; without a range filter on it the "
                f"query scans every partition. Add a condition such as "
                f"\"{column} >= DATE '2024-01-01'\". A bare IS NOT NULL does not narrow the scan."
            )
            raise GroundingError(msg)
