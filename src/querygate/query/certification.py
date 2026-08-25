"""Certified-only mode: accountability for the number, not access to the row.

A governed, perfectly correct ad-hoc query can still produce a figure that
disagrees with the board deck. Certification is therefore a separate axis: a
metric is certified when the data team signed off its definition in dbt
(``meta.certified``). QueryGate never sets it and the agent cannot claim it.

Refusals are a redirect rather than a wall: they name the certified metrics that
exist, so the agent asks an answerable question instead of guessing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from querygate import config

if TYPE_CHECKING:
    from querygate.auth.principal import Principal
    from querygate.catalog.models import Metric, SemanticCatalog


class CertificationError(ValueError):
    """Raised when a restricted principal asks for an uncertified number."""


def certified_only(principal: Principal | None) -> bool:
    """True when this caller may only read certified metrics."""
    if not config.CERTIFIED_ONLY_ROLES or principal is None:
        return False
    return any(principal.has_role(role) for role in config.CERTIFIED_ONLY_ROLES)


def _certified_names(catalog: SemanticCatalog) -> list[str]:
    return sorted(metric.name for metric in catalog.metrics if metric.certified)


def assert_free_sql_allowed(principal: Principal | None, catalog: SemanticCatalog) -> None:
    """Refuse ad-hoc SQL for a certified-only caller, pointing at what is allowed."""
    if not certified_only(principal):
        return
    available = _certified_names(catalog)
    listed = ", ".join(available) if available else "none are defined yet"
    msg = (
        "This account may only read certified metrics, so ad-hoc SQL is not available to it. "
        f"Use qg_get_metric with one of: {listed}. "
        "If the question needs a new definition, the data team owns that; ask them to certify it."
    )
    raise CertificationError(msg)


def assert_metric_allowed(principal: Principal | None, catalog: SemanticCatalog, metric: Metric) -> None:
    """Refuse an uncertified metric for a certified-only caller."""
    if not certified_only(principal) or metric.certified:
        return
    available = _certified_names(catalog)
    listed = ", ".join(available) if available else "none are defined yet"
    msg = (
        f"'{metric.name}' is not a certified metric, and this account may only read certified "
        f"numbers. Certified metrics: {listed}."
    )
    raise CertificationError(msg)
