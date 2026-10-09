import asyncio
from types import SimpleNamespace

import pytest
from telethon import errors

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.handlers.quality import handle_quality
from downloader_bot.bot.jobs.transfers import TransferJobs
from downloader_bot.bot.state.menus import MenuStore
from downloader_bot.core.config import Settings
from downloader_bot.schemas.media import Media, Quality, Source

QUALITY = Quality("original", "Original", "mp4", None, "mp4", "video/mp4", "progressive", "url")
MEDIA = Media("sample", "id", "Title", "", 1, "url", None, (QUALITY,))


def configure(monkeypatch):
    for key in ("API_ID", "API_HASH", "BOT_TOKEN", "DATABASE_URL"):
        monkeypatch.setenv(key, "1")
    for key in (
        "TRANSFER_CONCURRENCY",
        "MAX_CONCURRENT_REQUESTS",
        "CACHED_TRANSFER_CONCURRENCY",
        "USER_REQUEST_INTERVAL_SECONDS",
        "MAX_FILE_SIZE_MB",
        "METADATA_CONCURRENCY",
        "REMUX_CONCURRENCY",
        "UPLOAD_PARALLELISM",
        "UPLOAD_INFLIGHT_PARTS",
        "TRANSFER_TIMEOUT_SECONDS",
        "TELEGRAM_SENDS_PER_SECOND",
    ):
        monkeypatch.delenv(key, raising=False)


def test_default_settings_fit_a_bounded_1000_request_pipeline(monkeypatch):
    configure(monkeypatch)
    settings = Settings.from_environment()
    assert settings.max_requests == settings.concurrency == settings.cached_concurrency == 1000
    assert settings.request_interval == 60 and settings.max_file_bytes == 1024**3
    assert settings.upload_parallelism == 4 and settings.remux_concurrency == 2
    assert settings.sends_per_second == 0
    assert settings.upload_inflight_parts == 64
    assert settings.transfer_timeout == 3600


def test_blank_ffmpeg_setting_uses_the_executable_on_path(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("FFMPEG_PATH", "")
    assert Settings.from_environment().ffmpeg == "ffmpeg"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("UPLOAD_PARALLELISM", "9"),
        ("REMUX_CONCURRENCY", "1001"),
        ("USER_REQUEST_INTERVAL_SECONDS", "0"),
        ("TELEGRAM_SENDS_PER_SECOND", "-1"),
        ("MAX_FILE_SIZE_MB", "2000"),
        ("MAX_CONCURRENT_REQUESTS", "0"),
        ("MAX_FILE_SIZE_MB", "not-an-integer"),
        ("UPLOAD_INFLIGHT_PARTS", "0"),
        ("UPLOAD_INFLIGHT_PARTS", "1025"),
        ("TRANSFER_TIMEOUT_SECONDS", "59"),
        ("TRANSFER_TIMEOUT_SECONDS", "86401"),
        ("LOG_MAX_MB", "0"),
        ("LOG_MAX_MB", "1025"),
        ("LOG_BACKUP_COUNT", "0"),
        ("LOG_QUEUE_SIZE", "99"),
        ("LOG_STDOUT", "2"),
        ("METRICS_INTERVAL_SECONDS", "0"),
    ],
)
def test_invalid_resource_limits_fail_before_startup(monkeypatch, key, value):
    configure(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match=key):
        Settings.from_environment()


def test_thousand_remux_processes_can_be_configured_explicitly(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("REMUX_CONCURRENCY", "1000")
    assert Settings.from_environment().remux_concurrency == 1000


async def test_quality_handler_starts_a_task_without_a_cache_routing_lookup():
    menus = MenuStore()
    token = menus.add(1, 2, MEDIA)
    started, release = asyncio.Event(), asyncio.Event()

    async def answer(*args, **kwargs):
        pass

    async def respond(text, **kwargs):
        return None

    async def peer():
        return None

    async def deliver(*args):
        started.set()
        await release.wait()

    event = SimpleNamespace(
        sender_id=1,
        chat_id=2,
        pattern_match=[None, token.encode(), b"0"],
        answer=answer,
        respond=respond,
        get_input_chat=peer,
    )
    async with TransferJobs() as jobs:
        await handle_quality(event, SimpleNamespace(deliver=deliver), menus, jobs)
        await asyncio.wait_for(started.wait(), 1)
        assert jobs.active_count == 1
        release.set()
        await jobs.join()


async def test_network_failure_is_not_retried_as_a_duplicate_send():
    calls = 0

    async def send():
        nonlocal calls
        calls += 1
        raise ConnectionError("ambiguous result")

    delivery = TelegramDelivery(None, None, "")
    with pytest.raises(ConnectionError):
        await delivery._request(send, publish=True)
    assert calls == 1


async def test_explicit_flood_wait_is_shared_and_retried_without_blocking(monkeypatch):
    clock = [100.0]
    waiting, release = asyncio.Event(), asyncio.Event()
    calls = []
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        waiting.set()
        await release.wait()
        clock[0] += seconds
        await original_sleep(0)

    monkeypatch.setattr(
        "downloader_bot.bot.delivery.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    monkeypatch.setattr("downloader_bot.bot.delivery.asyncio.sleep", sleep)
    delivery = TelegramDelivery(None, None, "")

    async def first():
        calls.append("first")
        if calls.count("first") == 1:
            raise errors.FloodWaitError(request=None, capture=17)
        return 1

    async def second():
        calls.append("second")
        return 2

    task = asyncio.create_task(delivery._request(first, publish=True))
    await waiting.wait()
    other = asyncio.create_task(delivery._request(second, publish=True))
    await original_sleep(0)
    assert calls == ["first"]
    release.set()
    assert await task == 1 and await other == 2
    assert calls.count("first") == 2 and calls.count("second") == 1


async def test_only_two_remux_streams_can_run_and_waiters_are_cancellable(monkeypatch):
    active = peak = 0
    started, finish = asyncio.Event(), asyncio.Event()

    async def chunks(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            started.set()
        try:
            await finish.wait()
            yield b"test"
        finally:
            active -= 1

    async def upload(client, chunks, name, **kwargs):
        async for _ in chunks:
            pass
        return "uploaded"

    async def send_file(*args, **kwargs):
        return SimpleNamespace(
            document=SimpleNamespace(id=1, access_hash=2, file_reference=b"ref"),
            id=3,
        )

    monkeypatch.setattr("downloader_bot.bot.delivery.media_chunks", chunks)
    monkeypatch.setattr("downloader_bot.bot.delivery.upload_stream", upload)
    from telethon import types

    delivery = TelegramDelivery(SimpleNamespace(send_file=send_file), None, "ffmpeg")
    source = Source("url", "hls")
    tasks = [
        asyncio.create_task(
            delivery.new_file(
                types.InputPeerUser(1, 2),
                MEDIA,
                QUALITY,
                source,
            )
        )
        for _ in range(10)
    ]
    await asyncio.wait_for(started.wait(), 1)
    assert active == peak == 2
    tasks[-1].cancel()
    finish.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[-1], asyncio.CancelledError)
    assert active == 0 and peak == 2


async def test_stop_during_telegram_cooldown_does_not_publish_a_message(monkeypatch):
    from downloader_bot.bot.progress import TransferProgress

    waiting = asyncio.Event()

    async def sleep(seconds):
        waiting.set()
        await asyncio.Future()

    async def send_file(*args, **kwargs):
        pytest.fail("Cancelled pacing wait must not publish")

    monkeypatch.setattr("downloader_bot.bot.delivery.asyncio.sleep", sleep)
    delivery = TelegramDelivery(SimpleNamespace(send_file=send_file), None, "")
    delivery._retry_at = 10**20
    progress = TransferProgress()
    async with TransferJobs() as jobs:
        job = jobs.reserve(1, 2, progress)
        jobs.start(job, lambda: delivery._send_file(None, None, progress=progress))
        await waiting.wait()
        assert progress.phase == "waiting_telegram"
        assert jobs.cancel(job.token, 1, 2)
        await jobs.join()
    assert progress.phase == "cancelled"
