import os
import uuid

import asyncpg
import pytest
from redis.asyncio import Redis

from downloader_bot.database import FileRepository
from downloader_bot.models import TelegramFile


async def test_xvideos_references_are_isolated_by_site_quality_and_bot_in_postgres_and_redis():
    dsn = os.environ.get("TEST_DATABASE_URL")
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not dsn or not redis_url:
        pytest.skip("Set TEST_DATABASE_URL and TEST_REDIS_URL for integration tests")
    schema = "test_xvideos_" + uuid.uuid4().hex
    connection = await asyncpg.connect(dsn)
    pool = None
    cache = Redis.from_url(redis_url)
    keys = []
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=2,
            server_settings={"search_path": schema},
        )
        repository = FileRepository(pool, cache, 123)
        await repository.initialize()
        for site, quality, document in (
            ("xvideos", "high", 1),
            ("xvideos", "low", 2),
            ("xvideos", "hls_1280x720_h264", 3),
            ("soundcloud", "high", 4),
        ):
            reference = TelegramFile(document, 5, b"reference", b"peer", 6)
            await repository.save(site, "demo", quality, reference)
            keys.append(repository.key(site, "demo", quality))
            assert await repository.get(site, "demo", quality) == reference
            assert await cache.get(keys[-1])
        assert await pool.fetchval("SELECT count(*) FROM media_files") == 4
        restarted = FileRepository(pool, None, 123)
        for quality, document in (("high", 1), ("low", 2), ("hls_1280x720_h264", 3)):
            assert (await restarted.get("xvideos", "demo", quality)).document_id == document
        assert await FileRepository(pool, cache, 124).get("xvideos", "demo", "high") is None
        for number in range(10):
            await pool.execute(
                "INSERT INTO site_accounts(site,label,credential_env) VALUES($1,$2,$3)",
                "xvideos",
                f"account-{number}",
                f"XVIDEOS_ACCOUNT_{number}_COOKIE",
            )
        assert len(await repository.accounts("xvideos")) == 10
        assert not await repository.accounts("soundcloud")
    finally:
        if keys:
            await cache.delete(*keys)
        await cache.aclose()
        if pool is not None:
            await pool.close()
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
