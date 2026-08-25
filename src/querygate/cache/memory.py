from __future__ import annotations

import time
from collections import OrderedDict


class MemoryCache:
    def __init__(self, max_entries: int = 512) -> None:
        self._entries: OrderedDict[str, tuple[object, float]] = OrderedDict()
        self._max = max_entries

    async def get(self, key: str) -> object | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    async def set(self, key: str, value: object, ttl: int) -> None:
        if ttl <= 0:
            return
        self._entries[key] = (value, time.monotonic() + ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    async def close(self) -> None:
        self._entries.clear()
