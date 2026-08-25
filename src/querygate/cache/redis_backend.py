from __future__ import annotations

import json
from typing import TYPE_CHECKING

from querygate.log_setup import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

_log = get_logger("querygate.cache")


class RedisCache:
    def __init__(self, client: Redis, key_prefix: str = "qg:cache") -> None:
        self._client = client
        self._prefix = key_prefix

    async def get(self, key: str) -> object | None:
        try:
            raw = await self._client.get(f"{self._prefix}:{key}")
        except Exception as exc:
            _log.warning("cache read failed, treating as miss: %s", exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # Corrupt or schema-changed entry: ignore rather than fail the call.
            return None

    async def set(self, key: str, value: object, ttl: int) -> None:
        if ttl <= 0:
            return
        try:
            await self._client.set(f"{self._prefix}:{key}", json.dumps(value, default=str), ex=ttl)
        except Exception as exc:
            _log.warning("cache write failed, continuing uncached: %s", exc)

    async def close(self) -> None:
        await self._client.aclose()
