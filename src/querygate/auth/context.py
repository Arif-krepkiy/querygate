"""Request-scoped identity helpers."""

from __future__ import annotations

from querygate.auth.principal import Principal


def current_principal() -> Principal | None:
    from mcp.server.lowlevel.server import request_ctx

    from querygate.auth.token import QGAccessToken

    try:
        ctx = request_ctx.get()
    except LookupError:
        return None
    if ctx.request is None:
        return None
    user = getattr(ctx.request, "scope", {}).get("user")
    access = getattr(user, "access_token", None)
    if isinstance(access, QGAccessToken):
        return access.principal
    return None


def current_tenant_scopes() -> frozenset[str]:
    principal = current_principal()
    return principal.tenant_scopes if principal is not None else frozenset()


def ensure_auth() -> Principal:
    principal = current_principal()
    if principal is None:
        msg = (
            "No authenticated principal on this request. Call through the MCP transport with a Bearer token."
        )
        raise ValueError(msg)
    return principal
