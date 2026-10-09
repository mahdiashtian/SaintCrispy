from dataclasses import asdict

import pytest
from redis.exceptions import ConnectionError

from downloader_bot.repositories.redis.media import FileRepository, encode_file
from downloader_bot.schemas.media import TelegramFile

FILE = TelegramFile(1, 2, b"ref", b"peer", 3)


class FakePool:
    def __init__(self):
        self.file = None
        self.reads = 0
        self.fail_writes = False

    async def execute(self, sql, *args):
        if self.fail_writes:
            raise RuntimeError("database failed")
        self.file = TelegramFile(*args[4:])

    async def fetchrow(self, sql, *args):
        self.reads += 1
        return asdict(self.file) if self.file else None


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.fail = False

    async def get(self, key):
        if self.fail:
            raise ConnectionError("offline")
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        if self.fail:
            raise ConnectionError("offline")
        self.values[key] = value


async def test_cached_reads_do_not_reconnect_to_sql_and_quality_keys_are_distinct():
    pool, cache = FakePool(), FakeRedis()
    repo = FileRepository(pool, cache, 123)
    await repo.save("soundcloud", "track", "aac_160", FILE)
    assert await repo.get("soundcloud", "track", "aac_160") == FILE
    assert pool.reads == 0
    assert repo.key("soundcloud", "track", "aac_160") != repo.key("soundcloud", "track", "aac_96")


async def test_failed_cache_update_never_returns_old_cached_data(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("downloader_bot.repositories.redis.media.time.monotonic", lambda: clock[0])
    pool, cache = FakePool(), FakeRedis()
    repo = FileRepository(pool, cache, 123)
    key = repo.key("soundcloud", "track", "aac_160")
    cache.values[key] = encode_file(FILE)
    newer = TelegramFile(5, 6, b"new", b"peer", 7)
    cache.fail = True
    await repo.save("soundcloud", "track", "aac_160", newer)
    cache.fail = False
    assert await repo.get("soundcloud", "track", "aac_160") == newer
    assert pool.reads == 1
    # During cooldown SQL is authoritative; after cooldown the cache is repaired.
    clock[0] += 6
    assert await repo.get("soundcloud", "track", "aac_160") == newer
    assert pool.reads == 2
    assert await repo.get("soundcloud", "track", "aac_160") == newer
    assert pool.reads == 2


async def test_redis_outage_is_not_retried_for_every_new_key(monkeypatch):
    monkeypatch.setattr("downloader_bot.repositories.redis.media.time.monotonic", lambda: 100.0)
    pool, cache = FakePool(), FakeRedis()
    calls = 0

    async def offline(key):
        nonlocal calls
        calls += 1
        raise ConnectionError("offline")

    cache.get = offline
    repo = FileRepository(pool, cache, 123)
    for index in range(1000):
        assert await repo.get("sample", str(index), "original") is None
    assert calls == 1 and pool.reads == 1000


async def test_database_failure_is_not_published_to_redis():
    pool, cache = FakePool(), FakeRedis()
    pool.fail_writes = True
    repo = FileRepository(pool, cache, 123)
    with pytest.raises(RuntimeError):
        await repo.save("soundcloud", "track", "aac_160", FILE)
    assert not cache.values


async def test_restart_cannot_read_the_previous_process_stale_cache():
    pool, cache = FakePool(), FakeRedis()
    before = FileRepository(pool, cache, 123)
    after = FileRepository(pool, cache, 123)
    assert before.key("soundcloud", "track", "aac_160") != after.key(
        "soundcloud", "track", "aac_160"
    )
