"""Mapping a principal to a warehouse role, and refusing when it is ambiguous."""

from __future__ import annotations

import pytest

from querygate import config
from querygate.auth.principal import Principal
from querygate.query.identity import (
    IdentityError,
    resolve_warehouse_role,
    validate_configuration,
    warehouse_enforced,
)


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(config, "GOVERNANCE_MODE", "warehouse")
    monkeypatch.setattr(config, "WAREHOUSE_ROLE_MAP", {"analyst": "QG_ANALYST", "finance": "QG_FINANCE"})


def _principal(*roles: str) -> Principal:
    return Principal(subject="u@example.com", tenant_scopes=frozenset(), roles=frozenset(roles))


class TestMode:
    def test_inject_is_the_default(self):
        assert config.GOVERNANCE_MODE == "inject"
        assert warehouse_enforced() is False

    def test_enforced_mode_detected(self, enforced):
        assert warehouse_enforced() is True


class TestStartupValidation:
    def test_rejects_unknown_mode(self, monkeypatch):
        monkeypatch.setattr(config, "GOVERNANCE_MODE", "whatever")
        with pytest.raises(IdentityError, match="must be 'inject' or 'warehouse'"):
            validate_configuration()

    def test_enforced_mode_requires_a_role_map(self, monkeypatch):
        monkeypatch.setattr(config, "GOVERNANCE_MODE", "warehouse")
        monkeypatch.setattr(config, "WAREHOUSE_ROLE_MAP", {})
        with pytest.raises(IdentityError, match="QG_WAREHOUSE_ROLE_MAP"):
            validate_configuration()

    def test_inject_mode_needs_nothing(self):
        assert validate_configuration() is None


class TestRoleResolution:
    def test_inject_mode_uses_the_configured_identity(self):
        assert resolve_warehouse_role(_principal("analyst")) is None

    def test_maps_a_known_role(self, enforced):
        assert resolve_warehouse_role(_principal("analyst")) == "QG_ANALYST"

    def test_unmapped_role_is_refused(self, enforced):
        with pytest.raises(IdentityError, match="No warehouse role is mapped"):
            resolve_warehouse_role(_principal("intern"))

    def test_no_roles_at_all_is_refused(self, enforced):
        with pytest.raises(IdentityError, match="No warehouse role is mapped"):
            resolve_warehouse_role(_principal())

    def test_anonymous_is_refused(self, enforced):
        with pytest.raises(IdentityError, match="authenticated caller"):
            resolve_warehouse_role(None)

    def test_ambiguous_mapping_is_refused(self, enforced):
        """Picking one of several silently would grant or withhold by accident."""
        with pytest.raises(IdentityError, match="several warehouse roles"):
            resolve_warehouse_role(_principal("analyst", "finance"))

    def test_unmapped_roles_are_ignored_when_one_maps(self, enforced):
        assert resolve_warehouse_role(_principal("analyst", "everyone")) == "QG_ANALYST"


class TestPipelineSkipsInjection:
    def test_no_tenant_predicate_in_enforced_mode(self, enforced, catalog):
        from querygate.query.prepare import prepare_query

        prepared = prepare_query("SELECT region FROM customer_orders", catalog, frozenset())
        assert "qg_tenant_scopes" not in prepared.sql

    def test_empty_scopes_are_not_a_refusal_in_enforced_mode(self, enforced, catalog):
        """Scopes are meaningless here: the warehouse decides, not the token."""
        from querygate.query.prepare import prepare_query

        assert prepare_query("SELECT region FROM customer_orders", catalog, frozenset()) is not None


class TestSnowflakeRolePinning:
    """The session must be narrowed to one role and proved, not assumed.

    Snowflake activates every role granted to the user when
    DEFAULT_SECONDARY_ROLES is ALL, and this design grants the service user all
    mapped roles. Without `USE SECONDARY ROLES NONE` the primary role narrows
    nothing and the caller silently reads every audience's views.
    """

    class _Cursor:
        def __init__(self, current_role: str, log: list[str]):
            self._current_role, self._log, self._last = current_role, log, ""

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, *_args):
            self._log.append(sql)
            self._last = sql

        def fetchone(self):
            return (self._current_role,) if "CURRENT_ROLE" in self._last else None

    class _Conn:
        def __init__(self, current_role: str):
            self.statements: list[str] = []
            self.closed = False
            self._current_role = current_role

        def cursor(self):
            return TestSnowflakeRolePinning._Cursor(self._current_role, self.statements)

        def close(self):
            self.closed = True

    def test_disables_secondary_roles_before_switching(self):
        from querygate.warehouse.snowflake import _pin_role

        conn = self._Conn("QG_ANALYST")
        _pin_role(conn, "QG_ANALYST")
        assert conn.statements[0] == "USE SECONDARY ROLES NONE"
        assert conn.statements[1] == "USE ROLE QG_ANALYST"

    def test_verifies_the_role_actually_took_effect(self):
        from querygate.warehouse.snowflake import _pin_role
        from querygate.warehouse.types import WarehouseError

        conn = self._Conn("QG_FINANCE")  # silent fallback to another role
        with pytest.raises(WarehouseError, match="running as 'QG_FINANCE'"):
            _pin_role(conn, "QG_ANALYST")

    def test_rejects_a_role_name_that_is_not_an_identifier(self):
        from querygate.warehouse.snowflake import _pin_role
        from querygate.warehouse.types import WarehouseError

        conn = self._Conn("X")
        with pytest.raises(WarehouseError, match="not a valid Snowflake identifier"):
            _pin_role(conn, "QG_ANALYST; DROP ROLE X")
