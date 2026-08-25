from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from querygate import config, warehouse
from querygate.api import prompts
from querygate.api.errors import compact_json, error_json, with_tool_errors
from querygate.api.server import build_mcp
from querygate.auth.context import current_principal, current_tenant_scopes, ensure_auth
from querygate.cache import CacheKey, get_cache
from querygate.catalog.suggest import table_hint
from querygate.catalog.sync import get_catalog, get_index, request_reload
from querygate.obs import current_span, metrics, observed, set_query_attributes
from querygate.query import cursor as cursor_codec
from querygate.query.metrics import build_metric_query, resolve_metric
from querygate.query.prepare import PreparedQuery, prepare_filter_values_query, prepare_query
from querygate.query.profiling import build_profile_query, select_columns, shape_profile
from querygate.ratelimit import rate_limited
from querygate.results.compaction import compact_result
from querygate.results.provenance import build_provenance_footer
from querygate.retrieval.slices import metric_summary, model_detail, model_summary

mcp = build_mcp()
prompts.register(mcp)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)


async def _cost_check(prepared: PreparedQuery) -> float | None:
    """EXPLAIN and refuse above the plan-cost ceiling (0 disables)."""
    estimate = await warehouse.estimate(prepared)
    refused = bool(config.MAX_PLAN_COST and estimate.plan_cost > config.MAX_PLAN_COST)
    metrics.record_cost(config.WAREHOUSE, estimate.plan_cost, refused=refused)
    if refused:
        msg = (
            f"Query plan cost ~{estimate.plan_cost:,.0f} exceeds the configured ceiling. "
            f"Add a filter (often a date column) or select fewer columns."
        )
        raise warehouse.WarehouseError(msg)
    return estimate.plan_cost


@mcp.tool(annotations=_READ_ONLY)
@observed("search_catalog")
@with_tool_errors
@rate_limited("search_catalog")
async def qg_search_catalog(
    question: Annotated[
        str, Field(description="The analyst's question or keywords to search the catalog for.")
    ],
    limit: Annotated[int, Field(ge=1, le=50, description="Max models to return (default 10).")] = 10,
) -> str:
    """Find warehouse models relevant to a question (server-side hybrid retrieval).

    Call this FIRST to discover which models to query, then qg_describe_model
    for exact columns. Search in English and expand with synonyms; the server
    fuses keyword and semantic matching, so intent works even when the exact
    word is absent. One or two searches is enough; pick the best match and move on.

    The response also lists any defined METRICS on the matched models. A metric
    is a human-owned definition of a business term (e.g. "completed_revenue");
    prefer it and compute its `expr` (+ `filter`) rather than inventing your own.
    """
    ensure_auth()
    catalog = get_catalog()
    index = get_index()
    models = index.search(question, limit)
    metrics = [m for model in models for m in catalog.metrics_for(model.name)]
    return compact_json(
        {
            "models": [model_summary(m) for m in models],
            "metrics": [metric_summary(m) for m in metrics],
            "count": len(models),
        }
    )


@mcp.tool(annotations=_READ_ONLY)
@observed("describe_model")
@with_tool_errors
@rate_limited("describe_model")
async def qg_describe_model(
    name: Annotated[str, Field(description="Model name from search results.")],
) -> str:
    """Return one model's schema: columns with types, declared joins, and metrics.

    Use these names verbatim when writing SQL for qg_run_query.

    `joins` lists the join keys this project actually declares. Use them rather
    than guessing which columns relate two tables. If a metric is listed,
    compute it exactly as its `expr` + `filter` say; that is the agreed business
    definition, not a suggestion.
    """
    ensure_auth()
    catalog = get_catalog()
    model = catalog.get_model(name)
    if model is None:
        return error_json(
            f"Unknown model '{name}'.{table_hint(catalog, name)} Use qg_search_catalog to find valid names.",
            kind="grounding",
        )
    return compact_json(model_detail(model, catalog.metrics_for(model.name)))


@mcp.tool(annotations=_READ_ONLY)
@observed("get_filter_values")
@with_tool_errors
@rate_limited("get_filter_values")
async def qg_get_filter_values(
    table: Annotated[str, Field(description="Table to enumerate values from (must be an allowed schema).")],
    column: Annotated[
        str, Field(description="Column to list distinct values for; validated against the table.")
    ],
    search: Annotated[
        str | None,
        Field(
            default=None,
            max_length=config.MAX_SEARCH_LENGTH,
            description="Optional case-insensitive substring.",
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=config.MAX_ROW_LIMIT, description=f"Max values (default {config.DEFAULT_ROW_LIMIT})."),
    ] = config.DEFAULT_ROW_LIMIT,
) -> str:
    """List the real distinct values of a column so you filter on facts, not guesses.

    Runs a governed SELECT DISTINCT (the tenant filter is injected automatically).
    """
    ensure_auth()
    catalog = get_catalog()
    scopes = current_tenant_scopes()
    prepared = prepare_filter_values_query(table, column, catalog, scopes, search=search, limit=limit)

    # The cache key carries the tenant scopes, so two callers entitled to
    # different rows can never share an entry (see cache/base.py).
    cache = get_cache()
    key = CacheKey(
        namespace="filter_values",
        tenant_scopes=scopes,
        parts=(table, column, search or "", str(prepared.row_limit)),
    ).digest()

    values = await cache.get(key)
    if values is None:
        result = await warehouse.execute(prepared)
        values = [row.get("value") for row in result.rows]
        await cache.set(key, values, config.CACHE_TTL_SECONDS)

    return compact_json(
        {
            "table": table,
            "column": column,
            "values": values,
            "count": len(values),
            "truncated": len(values) >= prepared.row_limit,
        }
    )


@mcp.tool(annotations=_READ_ONLY)
@observed("get_table_stats")
@with_tool_errors
@rate_limited("get_table_stats")
async def qg_get_table_stats(
    table: Annotated[str, Field(description="Table to profile (must be in an allowed schema).")],
    columns: Annotated[
        list[str] | None,
        Field(default=None, description="Columns to profile; omit for the first few."),
    ] = None,
    sample_rows: Annotated[
        int, Field(ge=0, le=10, description="Example rows to include (0-10, default 3).")
    ] = 3,
) -> str:
    """Profile a table before querying it: row count, nulls, cardinality, ranges.

    Use this to understand the shape of the data (is this column mostly NULL? how
    many distinct values would a GROUP BY produce? what date range exists?)
    without pulling thousands of rows into context. Cheaper and more reliable
    than sampling by hand.

    Runs governed, so the numbers describe only the rows you may see.
    """
    ensure_auth()
    catalog = get_catalog()
    scopes = current_tenant_scopes()

    model = catalog.get_model(table)
    if model is None:
        return error_json(
            f"Unknown table '{table}'.{table_hint(catalog, table)} "
            f"Use qg_search_catalog to find valid names.",
            kind="grounding",
        )

    chosen = select_columns(model, columns)
    profile_query = build_profile_query(model, chosen, catalog, scopes)
    result = await warehouse.execute(profile_query)
    payload = shape_profile(result.rows[0] if result.rows else {}, chosen, model)
    payload["table"] = model.name

    if sample_rows:
        # model.name comes from the catalog (get_model resolved it), not from
        # the caller's string, and the result still goes through the full
        # validate → govern pipeline, so the sample is tenant-scoped too.
        sample_sql = f"SELECT * FROM {model.name}"  # noqa: S608 (catalog-resolved identifier)
        sample = prepare_query(sample_sql, catalog, scopes, limit=sample_rows)
        sampled = await warehouse.execute(sample)
        payload["sample"] = compact_result(sampled.columns, sampled.rows, row_limit=sample_rows)
    return compact_json(payload)


@mcp.tool(annotations=_READ_ONLY)
@observed("get_metric")
@with_tool_errors
@rate_limited("get_metric")
async def qg_get_metric(
    metric: Annotated[str, Field(description="Name of a defined metric, from search or describe.")],
    dimensions: Annotated[
        list[str] | None,
        Field(default=None, description="Columns to break the metric down by, e.g. ['region']."),
    ] = None,
    time_column: Annotated[
        str | None, Field(default=None, description="Date column to filter on when using start/end.")
    ] = None,
    start: Annotated[
        str | None, Field(default=None, description="Inclusive ISO date, e.g. 2024-01-01.")
    ] = None,
    end: Annotated[
        str | None, Field(default=None, description="Inclusive ISO date, e.g. 2024-06-30.")
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=config.MAX_ROW_LIMIT, description=f"Max rows (default {config.DEFAULT_ROW_LIMIT})."),
    ] = config.DEFAULT_ROW_LIMIT,
) -> str:
    """Compute a defined metric. Prefer this over writing the aggregation yourself.

    A metric is a business definition a human owns ("completed_revenue" counts
    only completed orders). Ask for it by name and the server builds the SQL
    from that definition, so your number matches what everyone else gets.

    Write your own SQL with qg_run_query only when no metric covers the question.
    """
    ensure_auth()
    catalog = get_catalog()
    scopes = current_tenant_scopes()

    definition = resolve_metric(catalog, metric)
    prepared = build_metric_query(
        catalog,
        definition,
        scopes,
        dimensions=dimensions,
        time_column=time_column,
        start=start,
        end=end,
        limit=limit,
    )
    await _cost_check(prepared)
    result = await warehouse.execute(prepared)
    payload = compact_result(result.columns, result.rows, row_limit=prepared.row_limit)
    payload["metric"] = {
        "name": definition.name,
        "label": definition.label,
        "definition": definition.expr,
        "filter": definition.filter,
        "certified": definition.certified,
    }
    return compact_json(payload)


@mcp.tool(annotations=_READ_ONLY)
@observed("run_query")
@with_tool_errors
@rate_limited("run_query")
async def qg_run_query(
    sql: Annotated[
        str,
        Field(
            min_length=1,
            max_length=config.MAX_SQL_LENGTH,
            description=(
                "A single read-only SELECT (or WITH … SELECT) over catalog tables. No DML/DDL, no "
                "multiple statements. The server injects the mandatory tenant filter; do not add it yourself."
            ),
        ),
    ],
    row_limit: Annotated[
        int,
        Field(
            ge=1,
            le=config.MAX_ROW_LIMIT,
            description=f"Max rows (default {config.DEFAULT_ROW_LIMIT}); clamped.",
        ),
    ] = config.DEFAULT_ROW_LIMIT,
    dry_run: Annotated[
        bool,
        Field(description="If true, validate + estimate cost without returning rows."),
    ] = False,
    cursor: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Continue a truncated result: pass the `next_cursor` from the previous page "
                "along with the SAME sql. Give your query a deterministic ORDER BY, or pages "
                "may overlap."
            ),
        ),
    ] = None,
) -> str:
    """Validate, govern, cost-check, and execute a read-only SELECT.

    Pipeline: ground against the catalog → inject + assert the tenant filter →
    EXPLAIN for cost → execute. Discover tables/columns with qg_search_catalog
    and qg_describe_model first. Answer the analyst by RUNNING this and
    presenting the result in plain business language, not the SQL or schema.
    """
    ensure_auth()
    catalog = get_catalog()
    scopes = current_tenant_scopes()

    offset = cursor_codec.decode(cursor, sql) if cursor else 0
    # Column-level security is bypassed only for a principal explicitly granted
    # the unmask role, never by anything the model can ask for.
    principal = current_principal()
    unmask = bool(config.PII_UNMASK_ROLE and principal and principal.has_role(config.PII_UNMASK_ROLE))
    prepared = prepare_query(sql, catalog, scopes, limit=row_limit, offset=offset, unmask=unmask)
    plan_cost = await _cost_check(prepared)

    # Structure only. The SQL text and tenant IDs are withheld unless
    # QG_TRACE_SENSITIVE is on. See obs/tracing.py for why.
    set_query_attributes(
        current_span(),
        tables=prepared.tables,
        tenant_count=len(prepared.tenant_scopes),
        plan_cost=plan_cost,
        sql=prepared.sql,
        tenant_scopes=prepared.tenant_scopes,
    )
    provenance = (
        build_provenance_footer(
            prepared.tables, catalog, plan_cost=plan_cost, tenant_scoped=bool(prepared.tenant_scopes)
        )
        if config.INCLUDE_PROVENANCE
        else None
    )

    if dry_run:
        payload: dict[str, object] = {
            "dry_run": True,
            "plan_cost": plan_cost,
            "row_limit": prepared.row_limit,
        }
        if provenance is not None:
            payload["provenance"] = provenance
        return compact_json(payload)

    result = await warehouse.execute(prepared)
    payload = compact_result(result.columns, result.rows, row_limit=prepared.row_limit)
    if payload["truncated"]:
        # Give the agent a way to continue instead of leaving it to guess that
        # more rows exist and re-running the whole query with a bigger limit.
        payload["next_cursor"] = cursor_codec.encode(sql, offset + prepared.row_limit)
    if provenance is not None:
        payload["provenance"] = provenance
    return compact_json(payload)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
@observed("reload_catalog")
@with_tool_errors
@rate_limited("reload_catalog")
async def qg_reload_catalog() -> str:
    """Trigger a background re-sync of the catalog from its source (also runs on a TTL).

    Non-blocking and deduplicated: repeated calls collapse into one in-flight build.
    """
    ensure_auth()
    started = request_reload(force=True)
    return compact_json({"status": "reload_started" if started else "reload_already_in_progress"})


@mcp.resource("schema://{model}")
def qg_model_schema(model: str) -> str:
    """One model's schema as a readable resource.

    Same content as qg_describe_model, reachable as a URI. Resources are how a
    client attaches context without spending a tool call; a user can pin
    `schema://customer_orders` to the conversation and the model has the columns
    before it asks for them.
    """
    catalog = get_catalog()
    found = catalog.get_model(model)
    if found is None:
        hint = table_hint(catalog, model).strip()
        return compact_json({"error": f"Unknown model '{model}'. {hint}".strip(), "kind": "grounding"})
    return compact_json(model_detail(found, catalog.metrics_for(found.name)))


@mcp.resource("querygate://catalog")
def qg_catalog_overview() -> str:
    """Every model name, domain and one-line description."""
    catalog = get_catalog()
    return compact_json(
        {
            "models": [model_summary(m) for m in catalog.models],
            "metrics": [metric_summary(m) for m in catalog.metrics],
            "build": catalog.build,
        }
    )


@mcp.resource("querygate://llm-instructions")
def qg_instructions() -> str:
    """The governed text-to-SQL workflow guidance for the agent."""
    from querygate.api.server import _instructions

    return _instructions()
