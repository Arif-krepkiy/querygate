from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from querygate.catalog.models import SemanticCatalog


def build_provenance_footer(
    tables: tuple[str, ...],
    catalog: SemanticCatalog,
    *,
    plan_cost: float | None,
    tenant_scoped: bool,
) -> dict[str, object]:
    domains = sorted({m.domain for t in tables if (m := catalog.get_model(t)) and m.domain})
    return {
        "tables": list(tables),
        "domains": domains,
        "tenant_scoped": tenant_scoped,
        "plan_cost": plan_cost,
        "catalog_build": catalog.build.get("git_sha", "unknown"),
    }
