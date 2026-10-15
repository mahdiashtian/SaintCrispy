"""Dedicated PostgreSQL/Redis checks for deadlines and deleted-origin references."""

import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from telethon import types

from bot.file_expiry import FileExpiry
from db.postgres.engine import migrate
from repositories.postgres.file_expiry import FileExpiryRepository
from repositories.redis.media import FileRepository
from schemas.media import TelegramFile

PEER = types.InputPeerUser(7, 123)
FILE = TelegramFile(1, 2, b"ref", bytes(PEER), 901, 100, True, True, True, True)


@pytest.fixture
async def expiry_database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Use a dedicated PostgreSQL database")
    schema = "expiry_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        url, min_size=1, max_size=8, server_settings={"search_path": schema}
    )
    try:
        await migrate(pool)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_deadlines_are_bot_scoped_idempotent_and_retries_preserve_original_time(
    expiry_database,
):
    pool = expiry_database
    first, other = FileExpiryRepository(pool, 123), FileExpiryRepository(pool, 456)
    deadline = datetime.now(UTC) - timedelta(seconds=1)
    await first.add(7, 901, bytes(PEER), deadline)
    await first.add(7, 901, bytes(PEER), deadline + timedelta(seconds=30))
    await other.add(7, 901, bytes(PEER), deadline)
    assert len(await first.due()) == len(await other.due()) == 1
    await first.defer(7, 901, 10)
    assert not await first.due() and len(await other.due()) == 1
    row = await pool.fetchrow("SELECT * FROM file_message_expiry WHERE telegram_account_id=123")
    assert row["delete_at"] == deadline and row["attempts"] == 1
    await first.finish(7, 901)
    await first.defer(7, 901, 20)
    assert (
        await pool.fetchval(
            "SELECT attempts FROM file_message_expiry WHERE telegram_account_id=123"
        )
        == 1
    )


async def test_restart_deletes_overdue_files_and_drops_only_the_deleted_origin_cache(
    expiry_database,
):
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Use a dedicated Redis instance")
    cache = Redis.from_url(redis_url)
    pool = expiry_database
    files = FileRepository(pool, cache, 123)
    other = FileRepository(pool, cache, 456)
    storage = replace(FILE, origin_peer=bytes(types.InputPeerChannel(99, 123)), message_id=100)
    try:
        await files.save("instagram", "reel", "720", FILE)
        await files.save("youtube", "video", "720", storage)
        await other.save("instagram", "reel", "720", FILE)
        assert await cache.get(files.key("instagram", "reel", "720"))
        await FileExpiryRepository(pool, 123).add(
            7, 901, bytes(PEER), datetime.now(UTC) - timedelta(seconds=1)
        )
        client = SimpleNamespace(delete_messages=AsyncMock())
        restored = FileExpiry(client, FileExpiryRepository(pool, 123), files)
        await restored.run_once()
        client.delete_messages.assert_awaited_once_with(PEER, [901], revoke=True)
        assert await files.get("instagram", "reel", "720") is None
        assert await cache.get(files.key("instagram", "reel", "720")) is None
        assert await files.get("youtube", "video", "720") == storage
        assert await other.get("instagram", "reel", "720") == FILE
        # A delayed producer cannot resurrect a deleted message as a cache anchor.
        await files.save("instagram", "reel", "720", FILE)
        assert await files.get("instagram", "reel", "720") is None
        fresh = replace(FILE, message_id=902)
        await files.save("instagram", "reel", "720", fresh)
        assert await files.get("instagram", "reel", "720") == fresh
    finally:
        await cache.delete(
            files.key("instagram", "reel", "720"),
            files.key("youtube", "video", "720"),
            other.key("instagram", "reel", "720"),
        )
        await cache.aclose()


async def test_redis_delete_failure_never_reuses_the_old_file(expiry_database):
    class Cache:
        values = {}

        async def get(self, key):
            return self.values.get(key)

        async def set(self, key, value, **options):
            self.values[key] = value

        async def delete(self, key):
            raise RedisConnectionError("offline")

    files = FileRepository(expiry_database, Cache(), 123)
    await files.save("pinterest", "pin", "original", FILE)
    await files.forget_origin(bytes(PEER), 901)
    assert await files.get("pinterest", "pin", "original") is None


async def test_only_old_completed_tombstones_are_pruned(expiry_database):
    pool = expiry_database
    repository = FileExpiryRepository(pool, 123)
    deadline = datetime.now(UTC) - timedelta(days=4)
    for identity in (901, 902):
        await repository.add(7, identity, bytes(PEER), deadline)
    await repository.finish(7, 901)
    await pool.execute(
        "UPDATE file_message_expiry SET completed_at=clock_timestamp()-interval '3 days' WHERE message_id=901"
    )
    assert [row["message_id"] for row in await repository.due()] == [902]
    assert await pool.fetchval("SELECT count(*) FROM file_message_expiry") == 1
