"""Rate limiter contract shared by every backend."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class RateLimitError(Exception):
    """Raised when a call is rejected because the caller is over their limit.

    Carries ``retry_after`` seconds so the agent can back off intelligently
    instead of hammering the server.
    """

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Decision:
    """Outcome of one limiter check."""

    allowed: bool
    tokens_left: float = 0.0
    retry_after: float = 0.0


class BaseLimiter(ABC):
    """Common interface for all rate limiter backends."""

    @abstractmethod
    async def check(self, key: str, cost: float = 1.0) -> Decision:
        """Return the decision for *key* without raising."""

    async def acquire(self, key: str, cost: float = 1.0) -> None:
        """Consume *cost* tokens for *key*, raising RateLimitError if over limit."""
        decision = await self.check(key, cost)
        if not decision.allowed:
            msg = (
                f"Rate limit exceeded. Retry in {decision.retry_after:.1f}s. "
                f"If you are running many queries, batch them into fewer, broader ones."
            )
            raise RateLimitError(msg, retry_after=decision.retry_after)

    async def close(self) -> None:
        """Release any backend resources. Overridden where it matters."""
        return


class NoopLimiter(BaseLimiter):
    """Used when rate limiting is switched off. Always allows."""

    async def check(self, key: str, cost: float = 1.0) -> Decision:
        return Decision(allowed=True)
