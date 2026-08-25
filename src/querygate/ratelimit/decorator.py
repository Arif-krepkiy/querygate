from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from querygate import config
from querygate.ratelimit.factory import get_limiter

_P = ParamSpec("_P")
_R = TypeVar("_R")


def resolve_bucket_key(tool_name: str) -> str:
    """Bucket key for the current request under the configured scope."""
    scope = config.RATE_LIMIT_SCOPE
    if scope == "global":
        return "global"

    # Imported lazily: this module is imported at tool-definition time, before
    # the MCP request machinery exists.
    from querygate.auth.context import current_principal

    principal = current_principal()
    if principal is None:
        # No identity (outside a request, or an unauthenticated probe). Bucket
        # them together rather than handing out an unlimited unkeyed pass.
        return "anonymous"

    if scope == "tenant":
        # Sorted so a multi-tenant caller maps to one stable bucket.
        tenants = ",".join(sorted(principal.tenant_scopes)) or "none"
        return f"tenant:{tenants}"
    return f"principal:{principal.subject}"


def tool_cost(tool_name: str) -> float:
    """Token cost of one call to *tool_name* (default 1.0)."""
    return config.RATE_LIMIT_COSTS.get(tool_name, 1.0)


def rate_limited(tool_name: str) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Charge the caller's bucket before running the tool.

    Raises :class:`RateLimitError`, which the tool error mapper turns into a
    ``kind="rate_limit"`` payload carrying a retry hint.
    """

    def decorate(func: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        @wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            await get_limiter().acquire(resolve_bucket_key(tool_name), tool_cost(tool_name))
            return await func(*args, **kwargs)

        return wrapper

    return decorate
