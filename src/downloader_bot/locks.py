import asyncio
from contextlib import asynccontextmanager


class KeyedLocks:
    """Only equal keys share a lock; release entries when the last waiter leaves."""

    def __init__(self):
        self._entries: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str):
        lock, users = self._entries.get(key, (asyncio.Lock(), 0))
        self._entries[key] = lock, users + 1
        try:
            async with lock:
                yield
        finally:
            _, users = self._entries[key]
            if users == 1:
                del self._entries[key]
            else:
                self._entries[key] = lock, users - 1
