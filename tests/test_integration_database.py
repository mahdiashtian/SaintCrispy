import asyncio
import os
import uuid

import asyncpg
import pytest
from redis.asyncio import Redis

from downloader_bot.database import FileRepository
from downloader_bot.models import TelegramFile


async def test_real_postgres_redis_quality_persistence_and_multiple_accounts():
    dsn = os.environ.get("TEST_DATABASE_URL")
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not dsn or not redis_url:
        pytest.skip("Set TEST_DATABASE_URL and TEST_REDIS_URL for integration tests")
    schema = "test_" + uuid.uuid4().hex
    connection = await asyncpg.connect(dsn)
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    cache = Redis.from_url(redis_url)
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        repo = FileRepository(pool, cache, 123)
        await repo.initialize()
        for quality, document in (("aac_160", 1), ("aac_96", 2)):
            file = TelegramFile(document, 3, b"ref", b"peer", 4, 1024)
            await repo.save("soundcloud", "2373831104", quality, file)
            assert await repo.get("soundcloud", "2373831104", quality) == file
        assert await pool.fetchval("SELECT count(*) FROM media_files") == 2
        for account in range(10):
            await pool.execute(
                "INSERT INTO site_accounts(site,label,credential_env) VALUES($1,$2,$3)",
                "soundcloud",
                f"test-{account}",
                f"SOUNDCLOUD_ACCOUNT_{account}_TOKEN",
            )
        assert len(await repo.accounts("soundcloud")) == 10
        restarted = FileRepository(pool, None, 123)
        assert (await restarted.get("soundcloud", "2373831104", "aac_96")).document_id == 2
        # SQL admission stays atomic across concurrent calls, restarts and Redis outages.
        results = await asyncio.gather(
            *(restarted.claim_request(77, "download", 60) for _ in range(30))
        )
        assert results.count(0) == 1 and all(1 <= value <= 60 for value in results if value)
        again = FileRepository(pool, None, 123)
        assert await again.claim_request(77, "download", 60) > 0
        assert await again.claim_request(78, "download", 60) == 0
        assert await again.claim_request(77, "inspect", 60) == 0
        assert await repo.claim_request(79, "download", 60) == 0
        assert await repo.claim_request(79, "download", 60) > 0
        await pool.execute("UPDATE request_limits SET requested_at=now()-interval '61 seconds'")
        assert await again.claim_request(77, "download", 60) == 0
        await cache.delete("request:123:79:download:60")
    finally:
        await pool.close()
        await cache.aclose()
        await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await connection.close()
