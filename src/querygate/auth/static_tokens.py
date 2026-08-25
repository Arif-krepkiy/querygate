"""Static bearer tokens: a fixed token maps to tenant scopes and roles."""

from __future__ import annotations

from mcp.server.auth.provider import TokenVerifier

from querygate import config
from querygate.auth.principal import Principal
from querygate.auth.token import QGAccessToken
from querygate.log_setup import get_logger

_log = get_logger("querygate.auth")


def parse_token_map(raw: str) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Parse ``"tok=acme|globex:admin,tok2=initech"`` into token → (tenants, roles).

    Roles are optional and follow a colon: ``tok=acme:admin|analyst``.
    """
    parsed: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        token, _, spec = entry.partition("=")
        tenant_part, _, role_part = spec.partition(":")
        tenants = frozenset(t.strip() for t in tenant_part.split("|") if t.strip())
        roles = frozenset(r.strip() for r in role_part.split("|") if r.strip())
        parsed[token.strip()] = (tenants, roles)
    return parsed


class StaticTokenVerifier(TokenVerifier):
    """Constant-time-ish lookup of a preconfigured token map."""

    def __init__(self, raw_tokens: str | None = None) -> None:
        self._tokens = parse_token_map(raw_tokens if raw_tokens is not None else config.DEMO_TOKENS)
        if not self._tokens:
            _log.warning("No static tokens configured. Every request will be unauthorized.")
        elif config.MCP_ENV not in ("local", "test"):
            _log.warning(
                "Static token auth is active in QG_ENV=%s. Static keys cannot be rotated per user "
                "and carry no expiry; use QG_AUTH_PROVIDER=oidc outside local development.",
                config.MCP_ENV,
            )

    async def verify_token(self, token: str) -> QGAccessToken | None:
        entry = self._tokens.get(token)
        if entry is None:
            return None
        tenants, roles = entry
        principal = Principal(
            subject=f"static:{token[:8]}",
            email=None,
            tenant_scopes=tenants,
            roles=roles,
            token=token,
        )
        return QGAccessToken(token=token, client_id="static", scopes=[], expires_at=None, principal=principal)
