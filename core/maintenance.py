"""Exclusive, reentrant maintenance gate for incoming updates and owned workers."""

import asyncio
import logging
from contextlib import asynccontextmanager

from core.diagnostics import error_fields
from schemas.media import DownloadError


class MaintenanceGate:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.exclusive = asyncio.Lock()
        self.active = {}
        self.paused = False
        self.waiters = set()
        self.ready = False

    @asynccontextmanager
    async def operation(self):
        task = asyncio.current_task()
        async with self.condition:
            while self.paused and task not in self.active:
                await self.condition.wait()
            self.active[task] = self.active.get(task, 0) + 1
        try:
            yield
        finally:
            async with self.condition:
                self.active[task] -= 1
                if not self.active[task]:
                    del self.active[task]
                self.condition.notify_all()

    @asynccontextmanager
    async def maintenance_lock(self):
        task = asyncio.current_task()
        async with self.condition:
            self.waiters.add(task)
            self.condition.notify_all()
        try:
            async with self.exclusive:
                yield
        finally:
            async with self.condition:
                self.waiters.discard(task)
                self.condition.notify_all()

    @asynccontextmanager
    async def guard(self, *, jobs, workers, memberships, groups, tracker, cache, bot_id):
        async with self.maintenance_lock():
            if not self.ready:
                raise DownloadError("راه‌اندازی ربات هنوز کامل نشده؛ کمی بعد دوباره امتحان کن.")
            task = asyncio.current_task()
            async with self.condition:
                self.paused = True
            stopped = []
            try:
                async with asyncio.timeout(60):
                    for worker in workers:
                        stopped.append(worker)
                        await worker.__aexit__(None, None, None)
                    async with self.condition:
                        while any(
                            owner is not task and owner not in self.waiters for owner in self.active
                        ):
                            await self.condition.wait()
                    if jobs.count:
                        raise DownloadError(
                            "برای عملیات داده، ابتدا صبر کن انتقال‌های فعال تمام شوند."
                        )
                    for service, stream, flush in (
                        (groups, groups.retry_queue, groups.flush_deferred),
                        (memberships, memberships.stream, memberships.flush),
                    ):
                        if stream is None:
                            continue
                        reclaim = stream.reclaim_ms
                        stream.reclaim_ms = 0
                        try:
                            for _ in range(100):
                                pending = sum(
                                    await asyncio.gather(
                                        *(
                                            stream.redis.xlen(stream.key(shard))
                                            for shard in range(stream.shards)
                                        )
                                    )
                                )
                                if not pending:
                                    break
                                await flush()
                            else:
                                raise DownloadError(
                                    "صف فعالیت هنوز خالی نشده؛ کمی بعد عملیات را تکرار کن."
                                )
                        finally:
                            stream.reclaim_ms = reclaim
                yield
            finally:
                try:
                    memberships.active.clear()
                    getattr(memberships, "profile_seen", {}).clear()
                    tracker._seen.clear()
                    tracker._checked.clear()
                    if cache and cache.redis is not None:
                        keys = []
                        async for key in cache.redis.scan_iter(
                            match=f"cache:v3:{bot_id}:*", count=200
                        ):
                            keys.append(key)
                            if len(keys) >= 200:
                                await cache.redis.delete(*keys)
                                keys.clear()
                        if keys:
                            await cache.redis.delete(*keys)
                except Exception as error:
                    if cache:
                        cache.retry_at = float("inf")
                    logging.getLogger("system").error(
                        {"event": "maintenance_cache_bypassed", **error_fields(error)}
                    )
                finally:
                    restart_errors = []
                    try:
                        for worker in stopped:
                            try:
                                await worker.__aenter__()
                            except Exception as error:
                                restart_errors.append(error)
                                logging.getLogger("system").error(
                                    {
                                        "event": "maintenance_worker_restart_failed",
                                        **error_fields(error),
                                    }
                                )
                    finally:
                        async with self.condition:
                            self.paused = False
                            self.condition.notify_all()
                    if restart_errors:
                        raise RuntimeError("Maintenance worker restart failed") from restart_errors[
                            0
                        ]
