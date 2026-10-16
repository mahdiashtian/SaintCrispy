"""Shared publication pacing; local fallback preserves async progress on outage."""

import asyncio
import logging
import time

from core.diagnostics import error_fields

log = logging.getLogger("telegram")


class TelegramRate:
    def __init__(self, repository=None, per_second=25):
        self.repository, self.interval = repository, 1 / per_second
        self.retry_at, self.next_at, self.redis_retry_at = 0.0, 0.0, 0.0
        self.lock = asyncio.Lock()

    async def reserve(self):
        async with self.lock:
            now = time.monotonic()
            if self.retry_at > now:
                return self.retry_at - now
            if self.repository is not None and now >= self.redis_retry_at:
                try:
                    return await self.repository.reserve(self.interval)
                except Exception as error:
                    self.redis_retry_at = now + 5
                    log.warning({"event": "telegram_rate_local_fallback", **error_fields(error)})
            delay = self.next_at - now
            if delay > 0:
                return delay
            self.next_at = now + self.interval
            return 0

    async def wait(self):
        while True:
            delay = await self.reserve()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    async def pause(self, seconds):
        self.retry_at = max(self.retry_at, time.monotonic() + seconds)
        if self.repository is not None:
            try:
                await self.repository.pause(seconds)
            except Exception as error:
                log.warning({"event": "telegram_pause_local_fallback", **error_fields(error)})
