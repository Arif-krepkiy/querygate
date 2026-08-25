"""Compile a SemanticCatalog from raw dbt artifacts (manifest + catalog)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from querygate import config
from querygate.catalog.models import (
    CatalogError,
    CatalogModel,
    Column,
    Join,
    Metric,
    SemanticCatalog,
)
from querygate.log_setup import get_logger

_log = get_logger("querygate.catalog.dbt")


def compile_dbt_target(target_dir: str, allowed_schemas: tuple[str, ...]) -> SemanticCatalog:
    target = Path(target_dir)
    manifest = _read_json(target / "manifest.json")
    catalog = _read_json(target / "catalog.json")
    if manifest is None or catalog is None:
        msg = f"dbt target '{target_dir}' is missing manifest.json and/or catalog.json."
        raise CatalogError(msg)

    glossary_raw = _read_json(target / "glossary.json") or {}
    catalog_nodes = catalog.get("nodes", {})

    models: list[CatalogModel] = []
    for node_id, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "model":
            continue
        # Ephemeral models (e.g. staging) are inlined into other models and are
        # not real relations and have no catalog.json entry. Skip them.
        if node.get("config", {}).get("materialized") == "ephemeral":
            continue
        # Internal dbt/MetricFlow helpers (time spine) are not analytics models.
        if node.get("name", "").startswith("metricflow_"):
            continue
        schema = node.get("schema", "")
        if allowed_schemas and schema not in allowed_schemas:
            continue
        cat_node = catalog_nodes.get(node_id, {})
        models.append(_to_model(node, cat_node))

    if not models:
        msg = f"No models in allowed schemas {allowed_schemas} found in the dbt target."
        raise CatalogError(msg)

    # Foreign keys come from dbt `relationships` tests, the only place a
    # project actually declares them.
    joins_by_model = _parse_relationship_tests(manifest)
    if joins_by_model:
        models = [replace(model, joins=tuple(joins_by_model.get(model.name, ()))) for model in models]

    # MetricFlow metrics are optional (dbt parse output). Absent → empty.
    metrics = _parse_semantic_manifest(_read_json(target / "semantic_manifest.json"))

    glossary = {k.lower(): tuple(v) for k, v in glossary_raw.items()}
    build = {
        "git_sha": str(manifest.get("metadata", {}).get("project_id", "unknown"))[:12],
        "generated_at": str(manifest.get("metadata", {}).get("generated_at", "")),
    }
    return SemanticCatalog(models=tuple(models), glossary=glossary, metrics=tuple(metrics), build=build)


_REF_NAME = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _parse_relationship_tests(manifest: dict) -> dict[str, list[Join]]:
    """Extract foreign keys from dbt ``relationships`` tests.

    Each such test says "this column must exist in that model's column", which
    is exactly a join key. Tolerant of shape drift: a test that does not parse
    is skipped rather than failing the catalog load.
    """
    joins: dict[str, list[Join]] = {}
    for node in manifest.get("nodes", {}).values():
        meta = node.get("test_metadata") or {}
        if meta.get("name") != "relationships":
            continue
        kwargs = meta.get("kwargs") or {}
        from_column = kwargs.get("column_name")
        to_column = kwargs.get("field")
        to_ref = _REF_NAME.search(str(kwargs.get("to", "")))
        from_ref = _REF_NAME.search(str(kwargs.get("model", "")))
        if not (from_column and to_column and to_ref and from_ref):
            continue
        joins.setdefault(from_ref.group(1), []).append(
            Join(to_model=to_ref.group(1), from_column=str(from_column), to_column=str(to_column))
        )
    return joins


def _parse_semantic_manifest(data: dict | None) -> list[Metric]:
    """Best-effort MetricFlow ingest: resolve each metric to the model + agg it
    is computed on. Tolerant of shape drift across dbt versions: on anything
    unexpected, skip that metric rather than fail the whole catalog load."""
    if not data:
        return []
    try:
        # measure name → (model_alias, "agg(expr)")
        measure_index: dict[str, tuple[str, str]] = {}
        for sm in data.get("semantic_models", []):
            alias = (sm.get("node_relation") or {}).get("alias") or sm.get("name", "")
            for measure in sm.get("measures", []):
                agg = measure.get("agg", "")
                expr = measure.get("expr") or measure.get("name", "")
                rendered = f"count(distinct {expr})" if agg == "count_distinct" else f"{agg}({expr})"
                measure_index[measure["name"]] = (alias, rendered)

        metrics: list[Metric] = []
        for m in data.get("metrics", []):
            measure_name = ((m.get("type_params") or {}).get("measure") or {}).get("name")
            if measure_name not in measure_index:
                continue
            model, expr = measure_index[measure_name]
            metrics.append(
                Metric(
                    name=m["name"],
                    label=m.get("label", m["name"]),
                    description=m.get("description", ""),
                    model=model,
                    expr=expr,
                    filter=_clean_filter(m),
                    certified=bool((m.get("meta") or {}).get("certified", False)),
                )
            )
        return metrics
    except (KeyError, TypeError, AttributeError) as exc:
        _log.warning("could not parse semantic_manifest.json, skipping metrics: %s", exc)
        return []


def _clean_filter(metric: dict) -> str:
    """Extract a human-readable filter from the MetricFlow where-filter Jinja."""
    filt = metric.get("filter") or {}
    wheres = filt.get("where_filters") or []
    templates = [w.get("where_sql_template", "") for w in wheres]
    # Strip the {{ Dimension('x__y') }} wrapper down to the bare column name.
    cleaned = " and ".join(t for t in templates if t)
    cleaned = re.sub(r"\{\{\s*Dimension\('[^_]*__([^']+)'\)\s*\}\}", r"\1", cleaned)
    return cleaned.strip()


def _to_model(node: dict, cat_node: dict) -> CatalogModel:
    name = node["name"]
    schema = node.get("schema", "")
    materialized = node.get("config", {}).get("materialized", "view")
    kind = "table" if materialized in ("table", "incremental") else "view"

    cat_cols = cat_node.get("columns", {})
    manifest_cols = node.get("columns", {})
    columns = tuple(
        Column(
            name=col_name,
            type=str(cat_cols.get(col_name, {}).get("type", "")),
            description=manifest_cols.get(col_name, {}).get("description", ""),
            # Declared in dbt as `meta: {mask: hash}`: a policy a human wrote,
            # not a guess from the column name.
            mask=(manifest_cols.get(col_name, {}).get("meta") or {}).get("mask"),
        )
        for col_name in (cat_cols or manifest_cols)
    )
    col_names = {c.name.lower() for c in columns}
    governed = config.TENANT_COLUMN.lower() in col_names
    partition_column = _partition_column(node)

    return CatalogModel(
        name=name,
        schema=schema,
        table=node.get("alias", name),
        kind=kind,
        governed=governed,
        tenant_column=config.TENANT_COLUMN if governed else None,
        domain=_domain_from_path(node.get("path", "")),
        description=node.get("description", ""),
        columns=columns,
        partition_column=partition_column,
    )


def _partition_column(node: dict) -> str | None:
    """Read dbt's `partition_by` config.

    BigQuery spells it as a dict (``{field, data_type, granularity}``); other
    adapters use a bare column name. Anything else is ignored rather than
    guessed at: a wrong partition column would refuse valid queries.
    """
    raw = (node.get("config") or {}).get("partition_by")
    if isinstance(raw, dict):
        field = raw.get("field")
        return str(field) if field else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _domain_from_path(path: str) -> str:
    parts = Path(path).parts
    # models/<domain>/foo.sql → <domain>; flat → "".
    return parts[-2] if len(parts) >= 2 else ""


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
