from __future__ import annotations

from typing import TYPE_CHECKING

from querygate import config
from querygate.log_setup import get_logger

if TYPE_CHECKING:
    from mcp.server.auth.provider import TokenVerifier

_log = get_logger("querygate.auth")


def create_verifier() -> TokenVerifier:
    provider = config.AUTH_PROVIDER
    if provider == "oidc":
        from querygate.auth.oidc import OIDCVerifier

        return OIDCVerifier()
    if provider in ("static", "demo", "apikey"):
        from querygate.auth.static_tokens import StaticTokenVerifier

        return StaticTokenVerifier()

    msg = f"Unknown QG_AUTH_PROVIDER '{provider}'. Supported: static, oidc."
    raise ValueError(msg)
