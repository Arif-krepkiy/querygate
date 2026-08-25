from __future__ import annotations

from sqlglot import exp

from querygate import config
from querygate.query.validation import parse_single


def clamp_row_limit(requested: int | None) -> int:
    if requested is None:
        return config.DEFAULT_ROW_LIMIT
    return max(1, min(requested, config.MAX_ROW_LIMIT))


def apply_offset(sql: str, offset: int) -> str:
    """Add an OFFSET so the agent can page through a large result.

    Offset paging is only stable if the query has a deterministic ORDER BY;
    without one the engine may return rows in a different order per page. The
    tool description tells the agent to order its query, which is the honest
    trade for keeping pagination stateless (no server-side cursors to expire).
    """
    if offset <= 0:
        return sql
    return parse_single(sql).offset(offset, dialect=config.SQL_DIALECT).sql(dialect=config.SQL_DIALECT)


def enforce_row_limit(sql: str, limit: int) -> str:
    """Add an outer LIMIT when absent; clamp a larger one down; leave a smaller
    explicit limit untouched. A non-literal LIMIT is replaced."""
    top = parse_single(sql)
    existing = top.args.get("limit")
    if isinstance(existing, exp.Limit) and isinstance(existing.expression, exp.Literal):
        try:
            current = int(existing.expression.name)
        except ValueError:
            current = limit + 1
        if current <= limit:
            return top.sql(dialect=config.SQL_DIALECT)
    return top.limit(limit, dialect=config.SQL_DIALECT).sql(dialect=config.SQL_DIALECT)
