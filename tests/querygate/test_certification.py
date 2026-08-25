"""Certified-only callers: no ad-hoc SQL, no uncertified metrics."""

from __future__ import annotations

from dataclasses import replace

import pytest

from querygate import config
from querygate.auth.principal import Principal
from querygate.catalog.models import Metric
from querygate.query.certification import (
    CertificationError,
    assert_free_sql_allowed,
    assert_metric_allowed,
    certified_only,
)


@pytest.fixture
def restricted(monkeypatch):
    monkeypatch.setattr(config, "CERTIFIED_ONLY_ROLES", ("exec",))
    return Principal(subject="ceo@example.com", tenant_scopes=frozenset({"acme"}), roles=frozenset({"exec"}))


@pytest.fixture
def analyst(monkeypatch):
    monkeypatch.setattr(config, "CERTIFIED_ONLY_ROLES", ("exec",))
    return Principal(
        subject="analyst@example.com", tenant_scopes=frozenset({"acme"}), roles=frozenset({"analyst"})
    )


def _metric(name: str, *, certified: bool) -> Metric:
    return Metric(
        name=name,
        label=name,
        description="",
        model="customer_orders",
        expr="sum(amount)",
        certified=certified,
    )


class TestWhoIsRestricted:
    def test_role_holder_is_restricted(self, restricted):
        assert certified_only(restricted) is True

    def test_other_roles_are_not(self, analyst):
        assert certified_only(analyst) is False

    def test_nobody_is_restricted_when_unset(self, monkeypatch, restricted):
        monkeypatch.setattr(config, "CERTIFIED_ONLY_ROLES", ())
        assert certified_only(restricted) is False

    def test_anonymous_is_not_restricted_here(self, restricted):
        # Auth is enforced upstream; this axis has nothing to say about it.
        assert certified_only(None) is False


class TestFreeFormSql:
    def test_refused_for_restricted_principal(self, restricted, catalog):
        with pytest.raises(CertificationError, match="only read certified metrics"):
            assert_free_sql_allowed(restricted, catalog)

    def test_allowed_for_everyone_else(self, analyst, catalog):
        assert assert_free_sql_allowed(analyst, catalog) is None

    def test_refusal_names_the_alternative(self, restricted, catalog):
        only_certified = replace(catalog, metrics=(_metric("completed_revenue", certified=True),))
        with pytest.raises(CertificationError, match="completed_revenue"):
            assert_free_sql_allowed(restricted, only_certified)


class TestMetricAccess:
    def test_certified_metric_allowed(self, restricted, catalog):
        assert assert_metric_allowed(restricted, catalog, _metric("revenue", certified=True)) is None

    def test_uncertified_metric_refused(self, restricted, catalog):
        with pytest.raises(CertificationError, match="not a certified metric"):
            assert_metric_allowed(restricted, catalog, _metric("draft_revenue", certified=False))

    def test_uncertified_metric_fine_for_analyst(self, analyst, catalog):
        assert assert_metric_allowed(analyst, catalog, _metric("draft_revenue", certified=False)) is None


class TestCatalogFlag:
    def test_defaults_to_uncertified(self):
        """Certification is opt-in: nothing is official until a human says so."""
        assert _metric("x", certified=False).certified is False
        assert Metric(name="x", label="x", description="", model="m", expr="sum(a)").certified is False
