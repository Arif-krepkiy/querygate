"""OIDC bearer token verification (Keycloak, Auth0, Okta)."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from typing import Any

from mcp.server.auth.provider import TokenVerifier

from querygate import config
from querygate.auth.principal import Principal
from querygate.auth.token import QGAccessToken
from querygate.log_setup import get_logger

_log = get_logger("querygate.auth.oidc")

# Asymmetric only. Allowing HS256 alongside RS256 enables the confusion attack
# where a token is signed with the provider's public key as an HMAC secret.
_ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")

# Re-validating a JWT on every tool call is wasted work inside one agent turn.
_TOKEN_CACHE_SECONDS = 60


def _claim_path(claims: dict[str, Any], path: str) -> Any:
    """Read a dotted claim path (``resource_access.querygate.roles``)."""
    node: Any = claims
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _as_str_set(value: Any) -> frozenset[str]:
    """Normalise a claim into a set of strings.

    Providers are inconsistent: a single tenant may arrive as a string, a list,
    or a space-separated string. All three mean the same thing.
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(part for part in value.replace(",", " ").split() if part)
    if isinstance(value, Iterable) and not isinstance(value, dict):
        return frozenset(str(item) for item in value if str(item))
    return frozenset()


class OIDCVerifier(TokenVerifier):
    """Validate JWTs against an OIDC provider's published keys."""

    def __init__(
        self,
        issuer: str | None = None,
        audience: str | None = None,
        jwks_client: Any = None,
    ) -> None:
        self._issuer = (issuer if issuer is not None else config.OIDC_ISSUER or "").rstrip("/")
        self._audience = audience if audience is not None else config.OIDC_AUDIENCE
        if not self._issuer:
            msg = "QG_OIDC_ISSUER must be set when QG_AUTH_PROVIDER=oidc."
            raise ValueError(msg)
        self._jwks_client = jwks_client  # injectable for tests
        self._cache: dict[str, tuple[float, QGAccessToken]] = {}
        _log.info("OIDC verifier: issuer=%s audience=%s", self._issuer, self._audience or "(unchecked)")

    # -- key material ----------------------------------------------------

    def _keys(self):
        if self._jwks_client is None:
            import jwt

            jwks_uri = self._discover_jwks_uri()
            # PyJWKClient caches keys and re-fetches when a kid is unknown, so
            # provider key rotation needs no restart here.
            self._jwks_client = jwt.PyJWKClient(jwks_uri, cache_keys=True)
        return self._jwks_client

    def _discover_jwks_uri(self) -> str:
        """Read ``jwks_uri`` from the issuer's discovery document.

        Discovering rather than hard-coding means a provider that moves its key
        endpoint (or a Keycloak realm rename) does not silently break auth.
        """
        import httpx

        url = f"{self._issuer}/.well-known/openid-configuration"
        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
        jwks_uri = response.json().get("jwks_uri")
        if not jwks_uri:
            msg = f"Discovery document at {url} has no jwks_uri."
            raise ValueError(msg)
        return str(jwks_uri)

    # -- verification ----------------------------------------------------

    async def verify_token(self, token: str) -> QGAccessToken | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(digest)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        try:
            import jwt

            signing_key = self._keys().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(_ALLOWED_ALGORITHMS),
                issuer=self._issuer,
                audience=self._audience or None,
                options={
                    "require": ["exp", "iss"],
                    "verify_aud": bool(self._audience),
                },
            )
        except Exception as exc:
            # Deliberately coarse: the caller learns "invalid token" and the
            # detail stays in our logs. Telling a client *why* a token failed
            # helps an attacker more than it helps a legitimate user.
            _log.info("token rejected: %s: %s", type(exc).__name__, exc)
            return None

        access = self._to_access_token(token, claims)
        if len(self._cache) > 1024:
            self._cache.clear()
        self._cache[digest] = (time.monotonic() + _TOKEN_CACHE_SECONDS, access)
        return access

    def _to_access_token(self, token: str, claims: dict[str, Any]) -> QGAccessToken:
        tenants = _as_str_set(_claim_path(claims, config.OIDC_TENANT_CLAIM))
        roles = _as_str_set(_claim_path(claims, config.OIDC_ROLES_CLAIM))
        if not tenants:
            # Fail-closed by construction: no tenancy in the token means the
            # governance layer will refuse governed reads. Log it, because in
            # practice it is nearly always a misconfigured protocol mapper.
            _log.warning(
                "token for sub=%s carries no '%s' claim; governed queries will be refused",
                claims.get("sub", "?"),
                config.OIDC_TENANT_CLAIM,
            )
        principal = Principal(
            subject=str(claims.get("sub", "")),
            email=claims.get("email"),
            tenant_scopes=tenants,
            roles=roles,
            token=token,
        )
        expires_at = int(claims["exp"]) if "exp" in claims else None
        return QGAccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or "oidc"),
            scopes=sorted(_as_str_set(claims.get("scope"))),
            expires_at=expires_at,
            principal=principal,
        )
