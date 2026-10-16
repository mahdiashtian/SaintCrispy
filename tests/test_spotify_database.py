"""Spotify menus, stable IDs and duplicate file writes on real SQL/Redis."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors
from test_admin_reference_database import app as app
from test_spotify_delivery import FRESH, PEER, STALE
from test_spotify_downloader import PAGE, TRACK, media

from bot.delivery import TelegramDelivery
from bot.state.menus import PersistentMenuStore
from repositories.postgres.media import PostgresMediaRepository
from repositories.postgres.workflow import WorkflowRepository
from repositories.redis.media import FileRepository
from schemas.media import DownloadError, Source, TelegramFile
from services.download import DownloadService


async def test_spotify_menu_keeps_sender_and_group_ownership_after_restart(app):
    store = PersistentMenuStore(app.workflow)
    value = media()
    token = await store.create(7, -10, value, message_id=42)
    restored = await PersistentMenuStore(app.workflow).fetch(token, 7, -10)
    assert restored.media.content_id == TRACK and restored.message_id == 42
    for owner, chat in ((8, -10), (7, -11)):
        with pytest.raises(DownloadError):
            await PersistentMenuStore(app.workflow).fetch(token, owner, chat)
    with pytest.raises(DownloadError):
        await PersistentMenuStore(WorkflowRepository(app.pool, app.bot + 1)).fetch(token, 7, -10)


async def test_spotify_parallel_file_writes_and_per_user_receipts_remain_unique(app):
    repository = FileRepository(app.pool, app.redis, app.bot)
    value = media()
    quality = value.qualities[0]
    file = TelegramFile(1, 2, b"ref", b"peer", 42, 3252222, verified_complete=True)
    await asyncio.gather(
        *(repository.save("spotify", TRACK, quality.key, file) for _ in range(200))
    )
    assert await app.pool.fetchval("SELECT count(*) FROM media_files") == 1
    for_user = range(1, 201)
    await asyncio.gather(
        *(repository.record_delivery(owner, value, quality, "reused") for owner in for_user)
    )
    assert await app.pool.fetchval("SELECT count(*) FROM user_media_history") == 200
    assert (
        await FileRepository(app.pool, app.redis, app.bot).get("spotify", TRACK, quality.key)
        == file
    )
    assert (
        await FileRepository(app.pool, app.redis, app.bot + 1).get("spotify", TRACK, quality.key)
        is None
    )


async def test_spotify_catalog_preserves_track_id_without_signed_sources(app):
    repository = PostgresMediaRepository(app.pool, app.bot)
    value = media()
    await repository.remember_media(("spotify:" + TRACK, PAGE + "?si=tracking"), value)
    restored = await repository.known_media("spotify:" + TRACK)
    assert restored.site == "spotify" and restored.content_id == TRACK
    assert restored.requires_refresh and restored.qualities[0].endpoint == ""
    stored = await app.pool.fetchval("SELECT metadata::text FROM media_catalog")
    assert "preview-30-seconds" not in stored and "private-source" not in stored


async def test_expired_spotify_reference_repair_updates_sql_and_redis_once(app):
    repository = FileRepository(app.pool, app.redis, app.bot)
    value = media()
    quality = value.qualities[0]
    await repository.save("spotify", TRACK, quality.key, STALE)
    arrived, ready = 0, asyncio.Event()

    async def send(peer, file, **options):
        nonlocal arrived
        if file.id.id == STALE.document_id:
            arrived += 1
            if arrived == 20:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            raise errors.FileReferenceExpiredError(None)
        return SimpleNamespace(id=903)

    client = SimpleNamespace(send_file=send, get_messages=AsyncMock(return_value=None))
    delivery = TelegramDelivery(client, None, "ffmpeg")
    delivery.new_file = AsyncMock(return_value=FRESH)
    provider = SimpleNamespace(resolve=AsyncMock(return_value=Source("url", "progressive")))
    service = DownloadService(provider, repository, delivery, cached_concurrency=20)
    results = await asyncio.gather(*(service.deliver(PEER, value, quality) for _ in range(20)))
    assert results.count("transferred") == 1 and results.count("reused") == 19
    delivery.new_file.assert_awaited_once()
    assert await repository.get("spotify", TRACK, quality.key) == FRESH
    assert (
        await FileRepository(app.pool, app.redis, app.bot).get("spotify", TRACK, quality.key)
        == FRESH
    )
    await repository.forget_origin(STALE.origin_peer, STALE.message_id)
    assert await repository.get("spotify", TRACK, quality.key) == FRESH
