"""Static tokens and OIDC signature verification.

The OIDC tests generate an RSA keypair and sign real JWTs, so the verification
path runs for real, offline.
"""

from __future__ import annotations

import time

import pytest

from querygate import config
from querygate.auth.principal import Principal
from querygate.auth.static_tokens import StaticTokenVerifier, parse_token_map

jwt = pytest.importorskip("jwt", reason="install querygate[oidc] to run the OIDC tests")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from querygate.auth.oidc import OIDCVerifier  # noqa: E402

ISSUER = "https://keycloak.example.com/realms/querygate"
AUDIENCE = "querygate"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return key, private_pem


@pytest.fixture
def verifier(keypair):
    key, _ = keypair

    class FakeJWKS:
        """Stands in for the provider's key endpoint, not for the verifier."""

        def get_signing_key_from_jwt(self, _token):
            class _Signing:
                pass

            signing = _Signing()
            signing.key = key.public_key()
            return signing

    return OIDCVerifier(issuer=ISSUER, audience=AUDIENCE, jwks_client=FakeJWKS())


def make_token(keypair, **overrides) -> str:
    _, private_pem = keypair
    claims = {
        "sub": "user-123",
        "email": "analyst@example.com",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 300,
        "tenant_ids": ["acme"],
        "realm_access": {"roles": ["analyst"]},
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, private_pem, algorithm="RS256")


class TestStaticTokens:
    def test_parses_tenants_and_roles(self):
        parsed = parse_token_map("tok=acme|globex:admin|analyst,other=initech")
        assert parsed["tok"] == (frozenset({"acme", "globex"}), frozenset({"admin", "analyst"}))
        assert parsed["other"] == (frozenset({"initech"}), frozenset())

    async def test_known_token_resolves(self):
        verifier = StaticTokenVerifier("tok_acme=acme")
        access = await verifier.verify_token("tok_acme")
        assert access is not None
        assert access.principal.tenant_scopes == frozenset({"acme"})

    async def test_unknown_token_rejected(self):
        assert await StaticTokenVerifier("tok_acme=acme").verify_token("nope") is None


class TestOIDCAccepts:
    async def test_valid_token(self, verifier, keypair):
        access = await verifier.verify_token(make_token(keypair))
        assert access is not None
        assert access.principal.subject == "user-123"
        assert access.principal.email == "analyst@example.com"
        assert access.principal.tenant_scopes == frozenset({"acme"})
        assert access.principal.roles == frozenset({"analyst"})

    async def test_tenant_claim_accepts_a_bare_string(self, verifier, keypair):
        """Providers are inconsistent; one tenant may arrive unlisted."""
        access = await verifier.verify_token(make_token(keypair, tenant_ids="acme"))
        assert access.principal.tenant_scopes == frozenset({"acme"})

    async def test_tenant_claim_accepts_space_separated(self, verifier, keypair):
        access = await verifier.verify_token(make_token(keypair, tenant_ids="acme globex"))
        assert access.principal.tenant_scopes == frozenset({"acme", "globex"})

    async def test_nested_roles_claim(self, verifier, keypair):
        access = await verifier.verify_token(make_token(keypair))
        assert "analyst" in access.principal.roles

    async def test_second_call_is_cached(self, verifier, keypair):
        token = make_token(keypair)
        first = await verifier.verify_token(token)
        assert await verifier.verify_token(token) is first


class TestOIDCRejects:
    """Each of these is a way a token could be forged, replayed, or stale."""

    async def test_expired(self, verifier, keypair):
        assert await verifier.verify_token(make_token(keypair, exp=int(time.time()) - 10)) is None

    async def test_wrong_issuer(self, verifier, keypair):
        assert await verifier.verify_token(make_token(keypair, iss="https://evil.example")) is None

    async def test_wrong_audience(self, verifier, keypair):
        """A token minted for another service in the same realm must not work here."""
        assert await verifier.verify_token(make_token(keypair, aud="other-service")) is None

    async def test_signed_by_a_different_key(self, verifier):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        forged = jwt.encode(
            {"sub": "x", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300},
            pem,
            algorithm="RS256",
        )
        assert await verifier.verify_token(forged) is None

    async def test_unsigned_alg_none(self, verifier):
        """`alg: none` is the oldest JWT attack; the algorithm allow-list blocks it."""
        unsigned = jwt.encode(
            {"sub": "x", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300},
            key="",
            algorithm="none",
        )
        assert await verifier.verify_token(unsigned) is None

    async def test_hmac_signed_with_public_key(self, verifier, keypair):
        """Algorithm confusion, the attack the allow-list exists to stop.

        The attacker takes the provider's *public* key (it is public) and uses
        it as an HMAC secret. A server that accepts both RS256 and HS256 would
        verify this happily. Built by hand because PyJWT refuses to sign it.
        """
        import base64
        import hashlib
        import hmac
        import json

        key, _ = keypair
        public_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        def b64(raw: bytes) -> bytes:
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload = b64(
            json.dumps(
                {"sub": "attacker", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300}
            ).encode()
        )
        signing_input = header + b"." + payload
        signature = b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
        forged = (signing_input + b"." + signature).decode()

        assert await verifier.verify_token(forged) is None

    async def test_missing_exp(self, verifier, keypair):
        assert await verifier.verify_token(make_token(keypair, exp=None)) is None

    async def test_garbage(self, verifier):
        assert await verifier.verify_token("not-a-jwt") is None


class TestOIDCTenancy:
    async def test_missing_tenant_claim_yields_empty_scope(self, verifier, keypair, monkeypatch):
        """Fail-closed: no tenancy in the token means no governed reads, never
        'unrestricted'."""
        monkeypatch.setattr(config, "OIDC_TENANT_CLAIM", "tenant_ids")
        access = await verifier.verify_token(make_token(keypair, tenant_ids=None))
        assert access is not None
        assert access.principal.tenant_scopes == frozenset()

    async def test_custom_claim_path(self, verifier, keypair, monkeypatch):
        monkeypatch.setattr(config, "OIDC_TENANT_CLAIM", "resource_access.querygate.tenants")
        token = make_token(keypair, resource_access={"querygate": {"tenants": ["globex"]}})
        access = await verifier.verify_token(token)
        assert access.principal.tenant_scopes == frozenset({"globex"})


class TestFactory:
    def test_static_by_default(self, monkeypatch):
        from querygate.auth.factory import create_verifier

        monkeypatch.setattr(config, "AUTH_PROVIDER", "static")
        assert isinstance(create_verifier(), StaticTokenVerifier)

    def test_oidc_requires_issuer(self, monkeypatch):
        from querygate.auth.factory import create_verifier

        monkeypatch.setattr(config, "AUTH_PROVIDER", "oidc")
        monkeypatch.setattr(config, "OIDC_ISSUER", "")
        with pytest.raises(ValueError, match="QG_OIDC_ISSUER"):
            create_verifier()

    def test_unknown_provider_is_explicit(self, monkeypatch):
        from querygate.auth.factory import create_verifier

        monkeypatch.setattr(config, "AUTH_PROVIDER", "ldap")
        with pytest.raises(ValueError, match="Supported"):
            create_verifier()


class TestPrincipal:
    def test_roles_helper(self):
        assert Principal("s", roles=frozenset({"admin"})).has_role("admin")
        assert not Principal("s").has_role("admin")
