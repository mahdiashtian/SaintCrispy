import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from downloader_bot.models import DownloadError, Media, Quality, Source, TelegramFile
from downloader_bot.request_context import request_user, user_request
from downloader_bot.service import DownloadService
from downloader_bot.streaming import upload_stream

QUALITY = Quality("original", "Original", "mp4", None, "mp4", "video/mp4", "progressive", "url")
MEDIA = Media("youtube", "jNQXAC9IVRw", "Title", "Author", 19, "page", None, (QUALITY,))
FILE = TelegramFile(1, 2, b"ref", b"peer", 3, 1000)


async def test_one_thousand_recipients_download_once_then_send_concurrently():
    stored = None
    downloaded = active = peak = 0
    all_followers, publish = asyncio.Event(), asyncio.Event()

    async def get(*args):
        return stored

    async def save(*args):
        nonlocal stored
        stored = args[-1]

    async def transfer(*args):
        nonlocal downloaded
        downloaded += 1
        await publish.wait()
        return FILE

    async def resend(*args):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        if active == 999:
            all_followers.set()
        try:
            await asyncio.wait_for(all_followers.wait(), 3)
            return FILE
        finally:
            active -= 1

    service = DownloadService(
        SimpleNamespace(resolve=AsyncMock(return_value=Source("url", "progressive"))),
        SimpleNamespace(key=lambda *args: "same", get=get, save=save),
        SimpleNamespace(new_file=transfer, resend=resend),
        concurrency=1000,
        cached_concurrency=1000,
    )
    tasks = [asyncio.create_task(service.deliver(i, MEDIA, QUALITY)) for i in range(1000)]
    await asyncio.sleep(0)
    assert downloaded == 1
    publish.set()
    results = await asyncio.gather(*tasks)
    assert results.count("transferred") == 1 and results.count("reused") == 999
    assert downloaded == 1 and peak == 999 and not service._locks._entries


@pytest.mark.parametrize("operation", ["inspect", "transfer"])
async def test_thousand_failed_followers_do_not_restart_the_same_origin_request(operation):
    ready, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fail(*args):
        nonlocal calls
        calls += 1
        ready.set()
        await release.wait()
        raise DownloadError("origin temporarily unavailable")

    service = DownloadService(
        SimpleNamespace(inspect=fail, resolve=fail),
        SimpleNamespace(
            key=lambda *args: "same",
            get=AsyncMock(return_value=None),
            known_media=AsyncMock(return_value=None),
        ),
        None,
        concurrency=1000,
    )

    async def request():
        if operation == "inspect":
            return await service.inspect("same-url")
        return await service.deliver(1, MEDIA, QUALITY)

    tasks = [asyncio.create_task(request()) for _ in range(1000)]
    await ready.wait()
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, DownloadError) for result in results)
    assert calls == 1 and len(service._failures) == 1
    # An expired cooldown admits a later attempt and is not a permanent failure cache.
    key = next(iter(service._failures))
    service._failures[key] = 0, "expired"
    with pytest.raises(DownloadError):
        await request()
    assert calls == 2


async def test_storage_copy_does_not_hold_the_creation_lock_during_recipient_sends():
    stored = None
    active = peak = 0
    all_recipients = asyncio.Event()

    async def get(*args):
        return stored

    async def save(*args):
        nonlocal stored
        stored = args[-1]

    async def resend(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 1000:
            all_recipients.set()
        try:
            await asyncio.wait_for(all_recipients.wait(), 3)
            return FILE
        finally:
            active -= 1

    producer = AsyncMock(return_value=FILE)
    service = DownloadService(
        SimpleNamespace(resolve=AsyncMock(return_value=Source("url", "progressive"))),
        SimpleNamespace(key=lambda *args: "same", get=get, save=save),
        SimpleNamespace(new_file=producer, resend=resend),
        storage_peer="storage",
        concurrency=1000,
        cached_concurrency=1000,
    )
    results = await asyncio.gather(*(service.deliver(i, MEDIA, QUALITY) for i in range(1000)))
    assert results.count("transferred") == 1 and results.count("reused") == 999
    assert producer.await_count == 1 and peak == 1000


async def test_user_context_tracks_every_view_including_cached_and_failed_inspections():
    repository = SimpleNamespace(
        record_link_view=AsyncMock(return_value="hash"),
        finish_link_view=AsyncMock(),
        known_media=AsyncMock(return_value=None),
        remember_media=AsyncMock(),
    )
    extractor = AsyncMock(return_value=MEDIA)
    service = DownloadService(SimpleNamespace(inspect=extractor), repository, None)
    with user_request(1):
        assert await service.inspect("link") == MEDIA
    with user_request(2):
        assert await service.inspect("link") == MEDIA
    assert request_user.get() is None
    assert extractor.await_count == 1 and repository.record_link_view.await_count == 2
    assert repository.finish_link_view.call_args_list[-1].args == (2, "hash", MEDIA)
    extractor.side_effect = DownloadError("unavailable")
    with user_request(3), pytest.raises(DownloadError):
        await service.inspect("bad-link")
    assert repository.finish_link_view.call_args_list[-1].args == (3, "hash")


async def test_durable_catalog_reuses_only_saved_qualities_without_origin_requests():
    known = replace(MEDIA, qualities=(replace(QUALITY, endpoint=""),))
    repository = SimpleNamespace(
        known_media=AsyncMock(return_value=known),
        remember_media=AsyncMock(),
    )
    extractor = AsyncMock(side_effect=AssertionError("Origin must not be fetched"))
    service = DownloadService(
        SimpleNamespace(inspect=extractor, cache_key=lambda _: "id"), repository, None
    )
    assert await service.inspect("short-link") == known
    assert await service.inspect("watch-link") == known
    repository.known_media.assert_awaited_once_with("id")
    extractor.assert_not_awaited()


async def test_mutable_alias_reassignment_rediscovers_content_instead_of_resending_the_old_file():
    repository = SimpleNamespace(
        known_media=AsyncMock(return_value=MEDIA), remember_media=AsyncMock()
    )
    first = replace(MEDIA, site="soundcloud", content_id="old-numeric-track")
    replacement = replace(first, content_id="new-numeric-track")
    extractor = AsyncMock(side_effect=[first, replacement])
    service = DownloadService(
        SimpleNamespace(inspect=extractor, cache_key=lambda url: url, catalog_key=lambda _: None),
        repository,
        None,
    )
    assert await service.inspect("same-mutable-slug") == first
    service._metadata.clear()  # TTL expiry or process restart.
    assert await service.inspect("same-mutable-slug") == replacement
    repository.known_media.assert_not_awaited()
    repository.remember_media.assert_not_awaited()


async def test_delivery_history_is_written_for_every_recipient_only_after_success():
    repository = SimpleNamespace(
        key=lambda *args: "id",
        get=AsyncMock(return_value=FILE),
        record_delivery=AsyncMock(),
    )
    delivery = SimpleNamespace(resend=AsyncMock(return_value=FILE))
    service = DownloadService(None, repository, delivery, cached_concurrency=1000)

    async def deliver(user):
        with user_request(user):
            await service.deliver(user, MEDIA, QUALITY)

    await asyncio.gather(*(deliver(i) for i in range(1000)))
    assert {call.args[0] for call in repository.record_delivery.call_args_list} == set(range(1000))
    assert all(call.args[-1] == "reused" for call in repository.record_delivery.call_args_list)
    delivery.resend.side_effect = DownloadError("failed")
    with user_request(2000), pytest.raises(DownloadError):
        await service.deliver(2000, MEDIA, QUALITY)
    assert repository.record_delivery.await_count == 1000 and request_user.get() is None


async def test_global_upload_window_is_shared_by_thousand_streams_and_cancellation_releases_it(
    monkeypatch,
):
    monkeypatch.setattr("downloader_bot.streaming.PART_SIZE", 64)
    slots = asyncio.Semaphore(8)
    full, release = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def chunks():
        yield b"x" * 131

    async def client(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if slots._value == 0:
            full.set()
        try:
            await release.wait()
            return True
        finally:
            active -= 1

    tasks = [
        asyncio.create_task(
            upload_stream(
                client,
                chunks(),
                str(i),
                parallelism=4,
                upload_slots=slots,
            )
        )
        for i in range(1000)
    ]
    await asyncio.wait_for(full.wait(), 3)
    assert slots._value == 0 and 1 <= peak <= 8
    for task in tasks[:500]:
        task.cancel()
    await asyncio.gather(*tasks[:500], return_exceptions=True)
    release.set()
    results = await asyncio.gather(*tasks[500:])
    assert len(results) == 500 and peak <= 8 and active == 0 and slots._value == 8
