"""Print a query's rewrites, stage by stage, with the added text highlighted.

Calls the same ``_pipeline`` that ``run_query`` calls, with a recorder threaded
through it. The pipeline is pure, so this needs no warehouse and no server.

    uv run python examples/pipeline_trace.py                  # built-in tour
    uv run python examples/pipeline_trace.py "SELECT ..."     # your own SQL
"""

from __future__ import annotations

import difflib
import re
import sys

from querygate import config
from querygate.catalog.loaders import bundle
from querygate.query.prepare import trace_query

# Colour only when a terminal is watching, so piping into a file or a README
# yields clean text rather than escape soup.
_COLOUR = sys.stdout.isatty()


def _c(code: str) -> str:
    return code if _COLOUR else ""


DIM = _c("\033[2m")
BOLD = _c("\033[1m")
RED = _c("\033[31m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
CYAN = _c("\033[36m")
ADDED = _c("\033[42;30m")  # green background for text the server added
RESET = _c("\033[0m")
# Without colour the additions still need marking, or the whole point is lost.
MARK_OPEN, MARK_CLOSE = ("", "") if _COLOUR else ("«", "»")

_TOKENS = re.compile(r"\S+|\s+")


def highlight_additions(before: str, after: str) -> str:
    """*after*, with everything absent from *before* on a green background."""
    old = _TOKENS.findall(before)
    new = _TOKENS.findall(after)
    out = []
    for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        chunk = "".join(new[j1:j2])
        if tag in ("insert", "replace") and chunk:
            chunk = f"{ADDED}{MARK_OPEN}{chunk}{MARK_CLOSE}{RESET}"
        out.append(chunk)
    return "".join(out)


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def trace(sql: str, scopes: set[str], catalog, *, title: str = "") -> None:
    print(f"\n{BOLD}{'━' * 78}{RESET}")
    if title:
        print(f"{BOLD}{title}{RESET}")
    print(f"{DIM}caller scopes: {sorted(scopes) or '(none)'}{RESET}\n")
    print(f"{CYAN}The agent wrote:{RESET}")
    print(indent(sql.strip()))

    stages, prepared, error = trace_query(sql, catalog, frozenset(scopes))

    previous = sql
    for stage in stages:
        if not stage.changed:
            # A stage that rewrote nothing is still worth naming: "governance
            # did nothing here" is a claim, not an omission.
            print(f"{DIM}  ✓ {stage.name:9} {stage.description}{RESET}")
            continue
        print(f"\n{YELLOW}▸ {stage.name}{RESET}  {DIM}{stage.description}{RESET}")
        print(indent(highlight_additions(previous, stage.sql)))
        previous = stage.sql

    if error is not None:
        print(f"\n{RED}✗ REFUSED: {type(error).__name__}{RESET}")
        print(indent(str(error), "    "))
        return

    print(f"\n{GREEN}✓ EXECUTED{RESET}  {DIM}tenant IDs bound as parameters, never in the text{RESET}")
    print(indent(f"{prepared.tenant_param_name} = {list(prepared.tenant_scopes)}", "    "))


TOUR = [
    (
        "Governed table: the tenant predicate is added after the model is done",
        "SELECT region, sum(amount) AS revenue FROM customer_orders GROUP BY region",
        {"acme"},
    ),
    (
        "PII: masked in the AST, and barred from predicates so it can't be probed",
        "SELECT customer_name, amount FROM customer_orders ORDER BY amount DESC",
        {"acme"},
    ),
    (
        "Public table: nothing to govern, and the trace says so",
        "SELECT plan_name, monthly_price FROM plan_catalog",
        {"acme"},
    ),
    (
        "Every scope, however deep: a governed table inside a CTE is still filtered",
        "WITH top AS (SELECT customer_id, sum(amount) AS spend FROM customer_orders GROUP BY 1) "
        "SELECT * FROM top ORDER BY spend DESC",
        {"acme"},
    ),
    (
        "Forgotten join condition: refused on shape, before the engine plans anything",
        "SELECT o.amount, p.tier FROM customer_orders o, plan_catalog p",
        {"acme"},
    ),
    (
        "Right column, wrong table: the refusal comes with the join keys",
        "SELECT o.customer_id, o.is_active FROM customer_orders o",
        {"acme"},
    ),
    (
        "Fail-closed: a caller with no scopes does not get zero rows, it gets refused",
        "SELECT sum(amount) FROM customer_orders",
        set(),
    ),
]


def main() -> None:
    catalog = bundle.load_bundle(config.CATALOG_LOCAL_PATH)
    if len(sys.argv) > 1:
        trace(sys.argv[1], {"acme"}, catalog, title="Your query")
        return
    print(f"\n{BOLD}QueryGate: what happens between the agent and the warehouse{RESET}")
    for title, sql, scopes in TOUR:
        trace(sql, scopes, catalog, title=title)
    print(f"\n{DIM}Pass your own SQL as an argument to trace it.{RESET}\n")


if __name__ == "__main__":
    main()
