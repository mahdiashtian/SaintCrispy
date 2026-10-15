"""Write-through file cache with SQL as the durable authority."""

import asyncio
import base64
import json
import math
import time
import uuid
from dataclasses import asdict, fields

from redis.exceptions import RedisError

from repositories.postgres.media import PostgresMediaRepository
from schemas.media import TelegramFile

from .base import read_object
from .keys import admission_key, media_key


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
        return media_key(self.namespace, site, content_id, quality)

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks[int(key[-8:], 16) % len(self._locks)]

    async def forget_origin(self, peer: bytes, message_id: int):
        for row in await super().origin_files(peer, message_id):
            key = self.key(row["site"], row["content_id"], row["quality"])
            async with self._lock(key):
                if not await super().forget_origin_file(
                    row["site"], row["content_id"], row["quality"], peer, message_id
                ):
                    continue
                self._dirty.add(key)
                if self.redis is None:
                    self._dirty.discard(key)
                else:
                    try:
                        await self._redis_call(lambda: self.redis.delete(key))
                    except RedisError:
                        self._redis_retry_at = time.monotonic() + 5
                    else:
                        self._dirty.discard(key)

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
                        return decode_file(cached, expected_key=key)
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
            if await super()._save_if_live(site, content_id, quality, file):
                await self._cache(key, file)

    async def _cache(self, key: str, file: TelegramFile) -> None:
        if self.redis is None:
            self._dirty.discard(key)
            return
        if time.monotonic() < self._redis_retry_at:
            self._dirty.add(key)
            return
        try:
            await self._redis_call(
                lambda: self.redis.set(key, encode_file(file, key=key), ex=86400)
            )
        except RedisError:
            self._redis_retry_at = time.monotonic() + 5
            self._dirty.add(key)
        else:
            self._dirty.discard(key)

    async def claim_request(self, user_id: int, scope: str, interval: int) -> int:
        """Atomic SQL admission; Redis can reject repeats but can never grant admission."""
        key = admission_key(self.bot_id, user_id, scope, interval)
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


def encode_file(file: TelegramFile, *, key: str | None = None) -> str:
    data = asdict(file)
    for field in ("file_reference", "origin_peer"):
        data[field] = base64.b64encode(data[field]).decode("ascii")
    return json.dumps({"key": key, "file": data} if key is not None else data)


def decode_file(raw: bytes | str, *, expected_key: str | None = None) -> TelegramFile:
    names = {field.name for field in fields(TelegramFile)}
    if expected_key is not None:
        envelope = read_object(raw, {"key", "file"})
        if envelope["key"] != expected_key:
            raise ValueError("Cache identity mismatch")
        data = envelope["file"]
        if not isinstance(data, dict) or set(data) != names:
            raise ValueError("Invalid cached file fields")
    else:
        data = read_object(raw, names)
    for field in ("document_id", "access_hash", "message_id"):
        if type(data[field]) is not int:
            raise ValueError("Invalid cached file integer")
    if data["document_id"] <= 0 or data["message_id"] <= 0:
        raise ValueError("Invalid cached file identity")
    size = data["size_bytes"]
    if size is not None and (type(size) is not int or size < 0):
        raise ValueError("Invalid cached file size")
    for field in ("video_streaming", "video_preview", "video_faststart"):
        if data[field] is not None and type(data[field]) is not bool:
            raise ValueError("Invalid cached video flag")
    if type(data["verified_complete"]) is not bool:
        raise ValueError("Invalid cached completeness flag")
    for field in ("file_reference", "origin_peer"):
        if not isinstance(data[field], str):
            raise ValueError("Invalid cached file bytes")
        data[field] = base64.b64decode(data[field], validate=True)
    return TelegramFile(**data)
