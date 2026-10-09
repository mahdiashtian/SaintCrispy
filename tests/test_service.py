import asyncio
from types import SimpleNamespace

import pytest

from downloader_bot.schemas.media import DownloadError, Media, Quality, Source, TelegramFile
from downloader_bot.services.download import DownloadService

QUALITY = Quality("aac_160", "AAC 160", "aac", 160, "m4a", "audio/mp4", "hls", "endpoint")
MEDIA = Media("soundcloud", "track", "Title", "Artist", 30, "url", None, (QUALITY,))
FILE = TelegramFile(1, 2, b"ref", b"peer", 3)


@pytest.mark.parametrize("protocol", ["hls", "progressive"])
async def test_all_transfer_methods_persist_and_duplicate_requests_reuse(protocol):
    state = {"file": None, "new": 0, "resends": 0, "resolved": 0}

    async def get(*args):
        return state["file"]

    async def save(site, identity, quality, file):
        assert (site, identity, quality) == ("soundcloud", "track", "aac_160")
        state["file"] = file

    async def resolve(*args):
        state["resolved"] += 1
        return Source("https://cdn.example/audio", protocol)

    async def new_file(*args):
        await asyncio.sleep(0.01)
        state["new"] += 1
        return FILE

    async def resend(*args):
        state["resends"] += 1
        return FILE

    repository = SimpleNamespace(key=lambda *args: "00000001", get=get, save=save)
    service = DownloadService(
        SimpleNamespace(resolve=resolve),
        repository,
        SimpleNamespace(new_file=new_file, resend=resend),
    )
    result = await asyncio.gather(
        service.deliver("peer", MEDIA, QUALITY), service.deliver("peer", MEDIA, QUALITY)
    )
    assert sorted(result) == ["reused", "transferred"]
    assert state == {"file": FILE, "new": 1, "resends": 1, "resolved": 1}


async def test_thousand_identical_inspections_share_one_extraction():
    calls = 0
    release = asyncio.Event()

    async def inspect(url):
        nonlocal calls
        calls += 1
        await release.wait()
        return MEDIA

    service = DownloadService(SimpleNamespace(inspect=inspect), None, None)
    tasks = [asyncio.create_task(service.inspect("same-url")) for _ in range(1000)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)
    assert results == [MEDIA] * 1000 and calls == 1
    assert service._metadata_count == 0 and not service._metadata_locks._entries


async def test_metadata_capacity_and_cancellation_release_waiters():
    started = asyncio.Event()

    async def inspect(url):
        started.set()
        await asyncio.Future()

    service = DownloadService(
        SimpleNamespace(inspect=inspect),
        None,
        None,
        metadata_capacity=2,
    )
    first = asyncio.create_task(service.inspect("same"))
    await started.wait()
    second = asyncio.create_task(service.inspect("same"))
    await asyncio.sleep(0)
    with pytest.raises(DownloadError, match="سقف"):
        await service.inspect("different")
    for task in (first, second):
        task.cancel()
    await asyncio.gather(first, second, return_exceptions=True)
    assert service._metadata_count == 0 and not service._metadata_locks._entries


async def test_cached_delivery_does_not_wait_for_a_download_slot():
    started, finish = asyncio.Event(), asyncio.Event()

    async def get(site, identity, quality):
        return FILE if identity == "cached" else None

    async def new_file(*args):
        started.set()
        await finish.wait()
        return FILE

    async def resend(*args):
        return FILE

    async def save(*args):
        pass

    async def resolve(*args):
        return Source("url", "progressive")

    service = DownloadService(
        SimpleNamespace(resolve=resolve),
        SimpleNamespace(key=lambda site, identity, quality: identity, get=get, save=save),
        SimpleNamespace(new_file=new_file, resend=resend),
        concurrency=1,
    )
    slow = asyncio.create_task(service.deliver("peer", MEDIA, QUALITY))
    await asyncio.wait_for(started.wait(), 1)
    cached = Media("soundcloud", "cached", "Title", "Artist", 30, "url", None, (QUALITY,))
    assert await asyncio.wait_for(service.deliver("peer", cached, QUALITY), 1) == "reused"
    assert not slow.done()
    finish.set()
    assert await slow == "transferred"


async def test_unrelated_content_keys_do_not_share_a_transfer_lock():
    active = 0
    both = asyncio.Event()

    async def get(*args):
        return None

    async def save(*args):
        pass

    async def resolve(*args):
        return Source("url", "progressive")

    async def new_file(*args):
        nonlocal active
        active += 1
        if active == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 1)
        return FILE

    service = DownloadService(
        SimpleNamespace(resolve=resolve),
        SimpleNamespace(
            key=lambda site, identity, quality: identity + "00000001", get=get, save=save
        ),
        SimpleNamespace(new_file=new_file),
        concurrency=2,
    )
    other = Media("soundcloud", "other", "Title", "Artist", 30, "url", None, (QUALITY,))
    await asyncio.gather(
        service.deliver("peer", MEDIA, QUALITY), service.deliver("peer", other, QUALITY)
    )
    assert active == 2 and not service._locks._entries
