"""Which warehouse identity a query runs as.

Only meaningful in ``warehouse`` mode, where the engine's own grants do the
separating. That works only if the query really runs as the caller: a shared
service role would hand everyone the union of what it can read, with no error
anywhere. So the mapping is mandatory and there is no fallback.

The map is an explicit allowlist rather than the token's role claim passed
through. A claim is attacker-influenced input, and forwarding it verbatim would
let a loose IdP config name any role in the warehouse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from querygate import config

if TYPE_CHECKING:
    from querygate.auth.principal import Principal


class IdentityError(PermissionError):
    """Raised when a caller cannot be mapped to a warehouse identity."""


def warehouse_enforced() -> bool:
    """True when the warehouse, not this server, applies row security."""
    return config.GOVERNANCE_MODE == "warehouse"


def validate_configuration() -> None:
    """Fail at startup rather than serve every caller as one identity.

    Called from ``main``: a misconfiguration here is not a request-time error to
    be handled, it is a deployment that must not accept traffic.
    """
    if config.GOVERNANCE_MODE not in {"inject", "warehouse"}:
        msg = f"QG_GOVERNANCE_MODE must be 'inject' or 'warehouse', got '{config.GOVERNANCE_MODE}'."
        raise IdentityError(msg)
    if warehouse_enforced() and not config.WAREHOUSE_ROLE_MAP:
        msg = (
            "QG_GOVERNANCE_MODE=warehouse requires QG_WAREHOUSE_ROLE_MAP "
            "(e.g. 'analyst=QG_ANALYST,finance=QG_FINANCE'). Without it every caller would run "
            "under the same service role and see everything that role can read."
        )
        raise IdentityError(msg)


def resolve_warehouse_role(principal: Principal | None) -> str | None:
    """The warehouse role this caller's query must run as.

    Returns None in ``inject`` mode (the adapter uses its configured identity).
    Raises in ``warehouse`` mode when the caller maps to nothing: refusing is the
    only safe answer, because the alternative is answering as somebody else.
    """
    if not warehouse_enforced():
        return None
    if principal is None:
        msg = "Warehouse-enforced governance requires an authenticated caller."
        raise IdentityError(msg)

    matched = sorted(
        config.WAREHOUSE_ROLE_MAP[role] for role in principal.roles if role in config.WAREHOUSE_ROLE_MAP
    )
    if not matched:
        msg = (
            "No warehouse role is mapped for this account, so the query cannot run as the right "
            "identity. Ask an administrator to map one of your roles in QG_WAREHOUSE_ROLE_MAP."
        )
        raise IdentityError(msg)
    if len(matched) > 1:
        # Picking one silently would grant or withhold access by accident.
        msg = (
            f"This account maps to several warehouse roles ({', '.join(matched)}), so the query "
            "identity is ambiguous. Map exactly one, or split the account."
        )
        raise IdentityError(msg)
    return matched[0]
