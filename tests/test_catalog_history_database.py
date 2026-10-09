import asyncio
import json
import os
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from downloader_bot.core.request_context import user_request
from downloader_bot.repositories.redis.media import FileRepository
from downloader_bot.schemas.media import Media, Quality, TelegramFile
from downloader_bot.services.download import DownloadService


async def test_real_database_catalog_history_admission_and_duplicate_file_are_atomic():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL integration tests")
    schema = "test_" + uuid.uuid4().hex
    connection = await asyncpg.connect(dsn)
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=8, server_settings={"search_path": schema}
    )
    q = Quality(
        "mp4_720",
        "720p",
        "h264",
        None,
        "mp4",
        "video/mp4",
        "progressive",
        "https://cdn.example/?signed=secret",
    )
    media = Media(
        "youtube",
        "jNQXAC9IVRw",
        "Title",
        "Author",
        19,
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        None,
        (q,),
        "credential",
        "account",
    )
    file = TelegramFile(1, 2, b"ref", b"peer", 3, 1024)
    try:
        repo = FileRepository(pool, None, 123)
        await repo.initialize()
        await repo.remember_media(("youtube:jNQXAC9IVRw",), media)
        assert await repo.known_media("youtube:jNQXAC9IVRw") is None
        await asyncio.gather(
            *(repo.save(media.site, media.content_id, q.key, file) for _ in range(1000))
        )
        assert await pool.fetchval("SELECT count(*) FROM media_files") == 1
        metadata = await pool.fetchval("SELECT metadata FROM media_catalog")
        assert (
            "signed=secret" not in metadata
            and "credential" not in metadata
            and "account" not in metadata
        )
        assert json.loads(metadata)["qualities"][0]["endpoint"] == ""
        restarted = FileRepository(pool, None, 123)
        known = await restarted.known_media("youtube:jNQXAC9IVRw")
        assert known.content_id == media.content_id and known.qualities[0].key == q.key
        assert not known.qualities[0].endpoint and known.authorization is None
        extractor = AsyncMock(side_effect=AssertionError("Cache reuse must not contact YouTube"))
        delivery = SimpleNamespace(resend=AsyncMock(return_value=file))
        service = DownloadService(
            SimpleNamespace(cache_key=lambda _: "youtube:jNQXAC9IVRw", inspect=extractor),
            restarted,
            delivery,
            cached_concurrency=1000,
        )

        async def visit(user):
            with user_request(user):
                m = await service.inspect("https://youtu.be/jNQXAC9IVRw?si=example")
                assert await service.deliver(user, m, m.qualities[0]) == "reused"

        await asyncio.gather(*(visit(user) for user in range(1000)))
        assert await pool.fetchval("SELECT count(*) FROM user_link_history") == 1000
        assert await pool.fetchval("SELECT count(*) FROM user_media_history") == 1000
        assert (
            await pool.fetchval("SELECT count(*) FROM user_link_history WHERE status='inspected'")
            == 1000
        )
        assert await pool.fetchval("SELECT count(*) FROM media_files") == 1
        extractor.assert_not_awaited()
        await visit(1)
        assert await pool.fetchval("SELECT view_count FROM user_link_history WHERE user_id=1") == 2
        assert (
            await pool.fetchval("SELECT delivery_count FROM user_media_history WHERE user_id=1")
            == 2
        )
        assert await FileRepository(pool, None, 124).known_media("youtube:jNQXAC9IVRw") is None
        claims = await asyncio.gather(
            *(restarted.claim_request(2000, "inspect", 60) for _ in range(1000))
        )
        assert claims.count(0) == 1
        assert await FileRepository(pool, None, 123).claim_request(2000, "inspect", 60) > 0
        # Qualities without saved Telegram files never appear in a catalog-only menu.
        await restarted.remember_media(
            ("youtube:jNQXAC9IVRw",), replace(media, qualities=(q, replace(q, key="1080p")))
        )
        assert len((await restarted.known_media("youtube:jNQXAC9IVRw")).qualities) == 1
    finally:
        await pool.close()
        await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await connection.close()
