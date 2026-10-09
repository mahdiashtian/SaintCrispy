"""Write-through file cache with SQL as the durable authority."""

import asyncio
import base64
import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict

from redis.exceptions import RedisError

from downloader_bot.repositories.postgres.media import PostgresMediaRepository
from downloader_bot.schemas.media import TelegramFile


class FileRepository(PostgresMediaRepository):
    def __init__(self, pool, redis, bot_id: int):
        super().__init__(pool, bot_id)
        self.redis = redis
        self.bot_id = bot_id
        self.namespace = f"media:{bot_id}:{uuid.uuid4().hex}:"
        self._locks = [asyncio.Lock() for _ in range(128)]
        self._dirty: set[str] = set()
        self._redis_retry_at = 0.0
        self._redis_slots = asyncio.Semaphore(16)

    async def _redis_call(self, action):
        async with self._redis_slots:
            return await action()

    def key(self, site: str, content_id: str, quality: str) -> str:
        identity = json.dumps([site, content_id, quality], separators=(",", ":"))
        return self.namespace + hashlib.sha256(identity.encode()).hexdigest()

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks[int(key[-8:], 16) % len(self._locks)]

    async def get(self, site: str, content_id: str, quality: str) -> TelegramFile | None:
        key = self.key(site, content_id, quality)
        async with self._lock(key):
            if (
                self.redis is not None
                and key not in self._dirty
                and time.monotonic() >= self._redis_retry_at
            ):
                try:
                    cached = await self._redis_call(lambda: self.redis.get(key))
                    if cached:
                        return decode_file(cached)
                except RedisError:
                    self._redis_retry_at = time.monotonic() + 5
                    self._dirty.add(key)
                except (ValueError, KeyError, TypeError):
                    self._dirty.add(key)
            result = await super().get(site, content_id, quality)
            if result is None:
                # Negative results are not cached. Keep a failed key dirty if stale data exists.
                return None
            await self._cache(key, result)
            return result

    async def save(self, site: str, content_id: str, quality: str, file: TelegramFile) -> None:
        key = self.key(site, content_id, quality)
        async with self._lock(key):
            self._dirty.add(key)
            await super().save(site, content_id, quality, file)
            await self._cache(key, file)

    async def _cache(self, key: str, file: TelegramFile) -> None:
        if self.redis is None:
            self._dirty.discard(key)
            return
        if time.monotonic() < self._redis_retry_at:
            self._dirty.add(key)
            return
        try:
            await self._redis_call(lambda: self.redis.set(key, encode_file(file), ex=86400))
        except RedisError:
            self._redis_retry_at = time.monotonic() + 5
            self._dirty.add(key)
        else:
            self._dirty.discard(key)

    async def claim_request(self, user_id: int, scope: str, interval: int) -> int:
        """Atomic SQL admission; Redis can reject repeats but can never grant admission."""
        key = f"request:{self.bot_id}:{user_id}:{scope}:{interval}"
        if self.redis is not None and time.monotonic() >= self._redis_retry_at:
            try:
                remaining = await self._redis_call(lambda: self.redis.pttl(key))
                if remaining > 0:
                    return math.ceil(remaining / 1000)
            except RedisError:
                self._redis_retry_at = time.monotonic() + 5
        remaining = await super().claim_request(user_id, scope, interval)
        ttl = remaining or interval
        if self.redis is not None and time.monotonic() >= self._redis_retry_at:
            try:
                await self._redis_call(lambda: self.redis.set(key, "1", ex=ttl))
            except RedisError:
                self._redis_retry_at = time.monotonic() + 5
        return remaining


def encode_file(file: TelegramFile) -> str:
    data = asdict(file)
    for field in ("file_reference", "origin_peer"):
        data[field] = base64.b64encode(data[field]).decode("ascii")
    return json.dumps(data)


def decode_file(raw: bytes | str) -> TelegramFile:
    data = json.loads(raw)
    for field in ("file_reference", "origin_peer"):
        data[field] = base64.b64decode(data[field], validate=True)
    return TelegramFile(**data)
