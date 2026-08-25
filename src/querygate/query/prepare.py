"""The pure pipeline: validate, guard, qualify, limit, govern, assert."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from querygate.catalog.suggest import column_hint, table_hint
from querygate.constants import SEARCH_PARAM_NAME, TENANT_PARAM_NAME
from querygate.query.governance import (
    GovernanceError,
    assert_tenant_filter_present,
    inject_tenant_filter,
)
from querygate.query.guardrails import assert_guardrails
from querygate.query.limits import apply_offset, clamp_row_limit, enforce_row_limit
from querygate.query.masking import apply_masking, assert_masking_applied
from querygate.query.qualify import qualify_tables
from querygate.query.trace import NULL_RECORDER, StageRecorder
from querygate.query.validation import GroundingError, validate_grounded

if TYPE_CHECKING:
    from querygate.catalog.models import CatalogModel, SemanticCatalog
    from querygate.query.trace import Recorder, Stage


@dataclass(frozen=True)
class PreparedQuery:
    """A validated, governed, limited query ready for estimate + execution."""

    sql: str
    tenant_param_name: str
    tenant_scopes: tuple[str, ...]
    row_limit: int
    tables: tuple[str, ...]
    scalar_params: tuple[tuple[str, str], ...] = ()
    offset: int = 0

    def bind_params(self) -> dict[str, object]:
        """Named parameters for the driver: only those the SQL actually uses.

        A public-table query carries no tenant predicate, so binding the tenant
        list anyway would pass an unused parameter. psycopg tolerates that;
        DuckDB rejects it outright, so the filtering is what keeps one
        ``PreparedQuery`` valid across engines.

        Matching on the name as a substring is safe because these names are
        reserved: ``validation.parse_single`` rejects agent SQL that mentions
        them, so they can only appear where the server put them.
        """
        candidates: dict[str, object] = {self.tenant_param_name: list(self.tenant_scopes)}
        candidates.update(dict(self.scalar_params))
        return {name: value for name, value in candidates.items() if name in self.sql}


def _touches_governed(handles: dict[str, CatalogModel]) -> bool:
    return any(model.governed for model in handles.values())


def _pipeline(
    sql: str,
    catalog: SemanticCatalog,
    tenant_scopes: frozenset[str],
    *,
    limit: int | None,
    check_reserved: bool,
    offset: int = 0,
    unmask: bool = False,
    guardrails: bool = True,
    recorder: Recorder = NULL_RECORDER,
) -> PreparedQuery:
    handles = validate_grounded(sql, catalog, check_reserved=check_reserved)
    recorder.record("validate", "Parsed; every table and column resolved against the catalog.", sql)
    if guardrails:
        assert_guardrails(sql, handles, check_reserved=check_reserved)
        recorder.record("guard", "Shape checked: no unconditioned join, no unpruned partition.", sql)
    row_limit = clamp_row_limit(limit)

    if _touches_governed(handles) and not tenant_scopes:
        msg = (
            "Governance violation: the query reads governed tables but the caller has no permitted "
            "tenant scopes. Refusing to execute (fail-closed)."
        )
        raise GovernanceError(msg)

    qualified = qualify_tables(sql, catalog)
    recorder.record("qualify", "Model names replaced with real schema-qualified relations.", qualified)
    limited = enforce_row_limit(qualified, row_limit)
    recorder.record("limit", f"Row limit clamped to the server maximum ({row_limit}).", limited)
    paged = apply_offset(limited, offset)
    recorder.record("offset", f"Paging applied (offset {offset}).", paged)
    # Column-level security runs before row-level so the governance predicate
    # (on the tenant column, which is never masked) is added to final SQL.
    masked = paged if unmask else apply_masking(paged, catalog, handles)
    recorder.record("mask", "Columns the catalog marks as PII wrapped in their mask.", masked)
    governed = inject_tenant_filter(masked, catalog)
    recorder.record("govern", "Tenant predicate injected into every governed scope.", governed)
    assert_tenant_filter_present(governed, catalog)
    if not unmask:
        assert_masking_applied(governed, catalog, handles)
    recorder.record(
        "assert", "Re-parsed from scratch; both predicates re-proved on the final text.", governed
    )

    return PreparedQuery(
        sql=governed,
        tenant_param_name=TENANT_PARAM_NAME,
        tenant_scopes=tuple(sorted(tenant_scopes)),
        row_limit=row_limit,
        tables=tuple(sorted({model.name for model in handles.values()})),
        offset=offset,
    )


def prepare_query(
    sql: str,
    catalog: SemanticCatalog,
    tenant_scopes: frozenset[str],
    *,
    limit: int | None = None,
    offset: int = 0,
    unmask: bool = False,
) -> PreparedQuery:
    """Run the pipeline over agent-supplied SQL.

    Raises GroundingError / GovernanceError with actionable messages.
    Fail-closed: governed tables + empty scopes → reject.
    """
    return _pipeline(
        sql, catalog, tenant_scopes, limit=limit, check_reserved=True, offset=offset, unmask=unmask
    )


def trace_query(
    sql: str,
    catalog: SemanticCatalog,
    tenant_scopes: frozenset[str],
    *,
    limit: int | None = None,
    offset: int = 0,
    unmask: bool = False,
) -> tuple[list[Stage], PreparedQuery | None, Exception | None]:
    """Run ``prepare_query`` and report what each stage did.

    Same code path as the real thing: the recorder is threaded through
    ``_pipeline`` rather than reimplemented, so a trace cannot drift from what
    executes. A refusal is returned rather than raised: the stages recorded
    before it are exactly what makes the refusal legible.
    """
    recorder = StageRecorder(original=sql)
    try:
        prepared = _pipeline(
            sql,
            catalog,
            tenant_scopes,
            limit=limit,
            check_reserved=True,
            offset=offset,
            unmask=unmask,
            recorder=recorder,
        )
    except (GroundingError, GovernanceError) as exc:
        return recorder.stages, None, exc
    return recorder.stages, prepared, None


def prepare_filter_values_query(
    table: str,
    column: str,
    catalog: SemanticCatalog,
    tenant_scopes: frozenset[str],
    *,
    search: str | None = None,
    limit: int | None = None,
) -> PreparedQuery:
    """Build + govern a ``SELECT DISTINCT <column>`` over one catalog table.

    Identifiers are catalog-validated; the optional substring binds as
    ``:qg_search`` (the value carries the ILIKE wildcards, the SQL does not).
    Runs the same governance pipeline as prepare_query.
    """
    model = catalog.resolve_table(table)
    if model is None:
        msg = (
            f"Unknown table '{table}'.{table_hint(catalog, table)} "
            f"Use qg_search_catalog to find valid model names."
        )
        raise GroundingError(msg)
    if column.lower() not in model.column_names():
        msg = (
            f"Column '{column}' is not in table '{model.name}'.{column_hint(catalog, model, column)} "
            f"Use qg_describe_model to see its columns."
        )
        raise GroundingError(msg)

    quoted = f'"{column}"'
    sql = f"SELECT DISTINCT {quoted} AS value FROM {model.name} WHERE {quoted} IS NOT NULL"  # noqa: S608
    scalar_params: tuple[tuple[str, str], ...] = ()
    if search:
        sql += f" AND CAST({quoted} AS TEXT) ILIKE :{SEARCH_PARAM_NAME}"
        scalar_params = ((SEARCH_PARAM_NAME, f"%{search}%"),)
    sql += " ORDER BY value"

    # Guardrails are skipped here because this SQL is not the agent's: it is one
    # table, one column, built from catalog-validated identifiers, so there is no
    # join to forget. The partition check would fire on a partitioned table and
    # leave the agent no way at all to discover valid filter values.
    prepared = _pipeline(sql, catalog, tenant_scopes, limit=limit, check_reserved=False, guardrails=False)
    return replace(prepared, scalar_params=scalar_params)
