"""Rewrite bare catalog model names to fully-qualified relations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from querygate import config
from querygate.query.validation import collect_cte_names, is_cte_reference, table_ref

if TYPE_CHECKING:
    from querygate.catalog.models import SemanticCatalog


def qualify_tables(sql: str, catalog: SemanticCatalog) -> str:
    """The agent writes ``monthly_revenue``; Postgres wants
    ``"analytics"."monthly_revenue"``. Aliases are preserved; the trailing
    identifier stays the implicit alias so tenant predicates still bind.
    CTE references are left untouched."""
    tree = sqlglot.parse_one(sql, dialect=config.SQL_DIALECT)
    cte_names = collect_cte_names(tree)
    for table in tree.find_all(exp.Table):
        if is_cte_reference(table, cte_names):
            continue
        model = catalog.resolve_table(table_ref(table))
        if model is None:
            continue
        qualified = sqlglot.to_table(model.relation(), dialect=config.SQL_DIALECT)
        alias = table.args.get("alias")
        if alias is not None:
            qualified.set("alias", alias.copy())
        table.replace(qualified)
    return tree.sql(dialect=config.SQL_DIALECT)
