from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Principal:
    """Identity resolved from a bearer token by whichever verifier is active.

    ``tenant_scopes`` is the only field the governance layer consults, and an
    empty set means "may read nothing governed", never "may read everything".
    Every verifier must uphold that: when tenancy cannot be determined, return
    an empty set rather than omitting the constraint.
    """

    subject: str
    email: str | None = None
    tenant_scopes: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    token: str = ""

    def has_role(self, role: str) -> bool:
        return role in self.roles
