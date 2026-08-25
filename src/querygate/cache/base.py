"""Cache contract.

The tenant scopes are part of the key type itself, not an optional field.
Two callers can send identical SQL and be owed different rows.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CacheKey:
    """An identity-scoped cache key.

    ``tenant_scopes`` has no default on purpose. See the module docstring.
    """

    namespace: str
    tenant_scopes: frozenset[str]
    parts: tuple[str, ...]

    def digest(self) -> str:
        """Stable string key. Tenant scopes are sorted so a multi-tenant caller
        maps to one key, and hashed in so they can never be spoofed by a
        crafted table or column name containing the separator."""
        tenants = "|".join(sorted(self.tenant_scopes)) or "<none>"
        material = "\x1f".join((tenants, *self.parts))
        return f"{self.namespace}:{hashlib.sha256(material.encode()).hexdigest()[:32]}"


class CacheBackend(Protocol):
    """Minimal async cache interface. Values must be JSON-serialisable."""

    async def get(self, key: str) -> object | None: ...
    async def set(self, key: str, value: object, ttl: int) -> None: ...
    async def close(self) -> None: ...


class NoopCache:
    """Used when caching is disabled. Never stores, never returns."""

    async def get(self, key: str) -> object | None:
        return None

    async def set(self, key: str, value: object, ttl: int) -> None:
        return

    async def close(self) -> None:
        return
