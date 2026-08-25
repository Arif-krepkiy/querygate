"""Load a SemanticCatalog from a compiled JSON bundle."""

from __future__ import annotations

import json
from pathlib import Path

from querygate.catalog.models import CatalogError, CatalogModel, Column, Join, Metric, SemanticCatalog


def parse_bundle(data: dict) -> SemanticCatalog:
    try:
        models = tuple(_parse_model(m) for m in data.get("models", []))
        metrics = tuple(_parse_metric(m) for m in data.get("metrics", []))
        glossary = {k.lower(): tuple(v) for k, v in (data.get("glossary") or {}).items()}
        build = {str(k): str(v) for k, v in (data.get("build") or {}).items()}
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"Malformed catalog bundle: {exc}"
        raise CatalogError(msg) from exc
    if not models:
        msg = "Catalog bundle contains no models."
        raise CatalogError(msg)
    return SemanticCatalog(models=models, glossary=glossary, metrics=metrics, build=build)


def _parse_metric(raw: dict) -> Metric:
    return Metric(
        name=raw["name"],
        label=raw.get("label", raw["name"]),
        description=raw.get("description", ""),
        model=raw["model"],
        expr=raw["expr"],
        filter=raw.get("filter", ""),
    )


def _parse_model(raw: dict) -> CatalogModel:
    columns = tuple(
        Column(
            name=c["name"],
            type=c.get("type", ""),
            description=c.get("description", ""),
            mask=c.get("mask"),
        )
        for c in raw.get("columns", [])
    )
    joins = tuple(
        Join(to_model=j["to_model"], from_column=j["from_column"], to_column=j["to_column"])
        for j in raw.get("joins", [])
    )
    return CatalogModel(
        name=raw["name"],
        schema=raw["schema"],
        table=raw.get("table", raw["name"]),
        kind=raw.get("kind", "table"),
        governed=bool(raw.get("governed", False)),
        tenant_column=raw.get("tenant_column"),
        domain=raw.get("domain", ""),
        description=raw.get("description", ""),
        columns=columns,
        joins=joins,
        partition_column=raw.get("partition_column"),
    )


def load_bundle(path: str) -> SemanticCatalog:
    text = Path(path).read_text(encoding="utf-8")
    return parse_bundle(json.loads(text))
