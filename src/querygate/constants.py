"""Static constants. Anything tunable per environment belongs in config.py."""

from __future__ import annotations

from typing import Final

SERVICE_NAME: Final[str] = "querygate"

# Named placeholder carrying the caller's tenant scopes. Governance injects a
# ``<col> = ANY(:qg_tenant_scopes)`` predicate referencing it; sqlglot renders
# it to psycopg's ``%(qg_tenant_scopes)s`` form and the warehouse binds the
# actual list at execution time. Tenant IDs are never interpolated into SQL.
TENANT_PARAM_NAME: Final[str] = "qg_tenant_scopes"

# Named placeholder for the get_filter_values substring (bound, not inlined).
SEARCH_PARAM_NAME: Final[str] = "qg_search"

# Raw SQL containing either reserved placeholder name is rejected up front, so
# a query can never smuggle text that collides with our bound parameters.
RESERVED_TOKENS: Final[tuple[str, ...]] = (TENANT_PARAM_NAME, SEARCH_PARAM_NAME)
