import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from telethon import types

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.handlers.limited import LimitedHandlers
from downloader_bot.bot.handlers.quality import handle_quality
from downloader_bot.bot.jobs.transfers import TransferJobs
from downloader_bot.bot.state.menus import MenuStore
from downloader_bot.bot.transfers.streaming import PART_SIZE, source_size, upload_stream
from downloader_bot.schemas.media import DownloadError, Media, Quality, Source, TelegramFile
from downloader_bot.services.limits import RequestLimiter

QUALITY = Quality("original", "Original", "mp4", None, "mp4", "video/mp4", "progressive", "url")
MEDIA = Media("sample", "id", "Title", "", 1, "url", None, (QUALITY,))
PEER = types.InputPeerUser(1, 2)


async def test_user_interval_is_atomic_and_expires_at_the_configured_second(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        "downloader_bot.services.limits.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    limiter = RequestLimiter(60)
    results = await asyncio.gather(
        *(limiter.check(1, "download") for _ in range(100)),
        return_exceptions=True,
    )
    assert results.count(None) == 1
    assert sum(isinstance(result, DownloadError) for result in results) == 99
    await limiter.check(2, "download")
    await limiter.check(1, "inspect")  # Showing qualities does not block selecting one.
    clock[0] += 59
    with pytest.raises(DownloadError, match="1 ثانیه"):
        await limiter.check(1, "download")
    clock[0] += 1
    await limiter.check(1, "download")


async def test_links_are_limited_across_chats_without_calling_the_provider_twice():
    registrations, inspected, responses = [], [], []
    client = SimpleNamespace(
        add_event_handler=lambda callback, builder: registrations.append(callback)
    )
    wrapper = LimitedHandlers(client, RequestLimiter(60))

    async def callback(event):
        inspected.append(event.chat_id)

    async def respond(text, **kwargs):
        responses.append(text)

    wrapper.add_event_handler(callback, None)
    for chat in (1, 2):
        await registrations[0](SimpleNamespace(sender_id=7, chat_id=chat, respond=respond))
    assert inspected == [1] and len(responses) == 1 and "60" in responses[0]


async def test_quality_rate_limit_releases_reservation_and_rejects_repeated_buttons():
    menus, limiter = MenuStore(), RequestLimiter(60)
    token = menus.add(1, 2, MEDIA)
    sent, answers = [], []

    async def answer(text, **options):
        answers.append((text, options))

    async def respond(*args, **options):
        return None

    async def peer():
        return PEER

    async def deliver(*args):
        sent.append(1)

    event = SimpleNamespace(
        sender_id=1,
        chat_id=2,
        pattern_match=[None, token.encode(), b"0"],
        answer=answer,
        respond=respond,
        get_input_chat=peer,
    )
    async with TransferJobs() as jobs:
        await handle_quality(event, SimpleNamespace(deliver=deliver), menus, jobs, limiter)
        await jobs.join()
        await handle_quality(event, SimpleNamespace(deliver=deliver), menus, jobs, limiter)
        assert jobs.count == 0 and sent == [1]
        assert answers[-1][1]["alert"] and "60" in answers[-1][0]


async def test_full_request_capacity_does_not_consume_user_interval():
    menus, limiter = MenuStore(), RequestLimiter(60)
    token = menus.add(1, 2, MEDIA)

    async def answer(*args, **kwargs):
        pass

    event = SimpleNamespace(
        sender_id=1,
        chat_id=2,
        pattern_match=[None, token.encode(), b"0"],
        answer=answer,
    )
    async with TransferJobs(capacity=1) as jobs:
        from downloader_bot.bot.progress import TransferProgress

        jobs.reserve(3, 4, TransferProgress())
        await handle_quality(event, None, menus, jobs, limiter)
        await limiter.check(1, "download")


async def test_known_oversize_source_is_rejected_before_any_network_request():
    delivery = TelegramDelivery(None, None, "", max_file_bytes=1024)
    with pytest.raises(DownloadError, match="حجم"):
        await delivery.new_file(PEER, MEDIA, QUALITY, Source("url", "progressive", size_bytes=1025))


async def test_external_file_size_is_verified_before_publishing():
    requests = []

    class Client:
        async def __call__(self, request):
            requests.append(request)
            return SimpleNamespace(
                document=SimpleNamespace(
                    id=1,
                    access_hash=2,
                    file_reference=b"ref",
                    size=1025,
                )
            )

        async def send_file(self, *args, **kwargs):
            pytest.fail("An oversized external file must not be published")

    delivery = TelegramDelivery(Client(), None, "", max_file_bytes=1024)
    with pytest.raises(DownloadError, match="حجم"):
        await delivery.new_file(PEER, MEDIA, QUALITY, Source("url", "progressive", size_bytes=1024))
    assert len(requests) == 1


async def test_source_size_reads_headers_and_uses_content_range_total():
    calls = []

    async def request(request):
        calls.append(request)
        if request.method == "HEAD":
            return httpx.Response(405)
        assert request.headers["range"] == "bytes=0-0"
        assert request.headers["referer"] == "https://example.org"
        return httpx.Response(
            206, headers={"Content-Range": "bytes 0-0/1234", "Content-Length": "1"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as http:
        size = await source_size(
            http,
            Source(
                "https://example.org/file",
                "progressive",
                headers={"Referer": "https://example.org"},
            ),
        )
    assert size == 1234 and len(calls) == 2


async def test_unknown_size_stream_is_capped_without_a_final_upload_part():
    closed, uploaded = asyncio.Event(), []

    async def chunks():
        try:
            yield b"x" * PART_SIZE
            yield b"x" * 2
        finally:
            closed.set()

    async def client(request):
        uploaded.append(request)
        return True

    with pytest.raises(DownloadError, match="حجم"):
        await upload_stream(client, chunks(), "file", max_file_bytes=PART_SIZE + 1)
    assert closed.is_set() and len(uploaded) == 1
    assert all(request.file_total_parts == -1 for request in uploaded)


async def test_unknown_size_url_uses_bounded_stream_instead_of_telegram_url_fetch(monkeypatch):
    requests = []

    async def response(request):
        requests.append(request.method)
        # Streaming responses intentionally omit Content-Length.
        return httpx.Response(200, stream=Body())

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 1025

    class Client:
        async def __call__(self, request):
            pytest.fail("Unknown-size URL must not be sent to Telegram for unrestricted fetching")

        async def send_file(self, *args, **kwargs):
            pytest.fail("Oversized stream must not publish")

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        delivery = TelegramDelivery(Client(), http, "", max_file_bytes=1024)
        with pytest.raises(DownloadError, match="حجم"):
            await delivery.new_file(
                PEER, MEDIA, QUALITY, Source("https://example.org/file", "progressive")
            )
    assert requests == ["HEAD", "GET", "GET"]


async def test_size_limit_allows_exact_boundary_and_records_the_reference_size():
    async def chunks():
        yield b"x" * 12

    async def client(request):
        return True

    handle = await upload_stream(client, chunks(), "file", max_file_bytes=12)
    assert handle.parts == 1


async def test_cached_oversized_file_is_rejected_before_send():
    file = TelegramFile(1, 2, b"ref", bytes(PEER), 3, 1025)
    with pytest.raises(DownloadError, match="حجم"):
        await TelegramDelivery(None, None, "", max_file_bytes=1024).resend(PEER, file, "")


async def test_legacy_cached_file_size_is_refreshed_and_persistable_before_send():
    file = TelegramFile(1, 2, b"old", bytes(PEER), 3)
    requests = []

    async def messages(peer, ids):
        requests.append("inspect")
        return SimpleNamespace(
            document=SimpleNamespace(
                id=1,
                access_hash=2,
                file_reference=b"new",
                size=1024,
            )
        )

    async def send(*args, **kwargs):
        requests.append("send")

    client = SimpleNamespace(get_messages=messages, send_file=send)
    fresh = await TelegramDelivery(client, None, "", max_file_bytes=1024).resend(PEER, file, "")
    assert fresh == replace(file, file_reference=b"new", size_bytes=1024)
    assert requests == ["inspect", "send"]


async def test_legacy_oversized_reference_is_rejected_after_refresh():
    async def messages(*args, **kwargs):
        return SimpleNamespace(
            document=SimpleNamespace(id=1, access_hash=2, file_reference=b"r", size=1025)
        )

    client = SimpleNamespace(get_messages=messages)
    with pytest.raises(DownloadError, match="حجم"):
        await TelegramDelivery(client, None, "", max_file_bytes=1024).resend(
            PEER,
            TelegramFile(1, 2, b"old", bytes(PEER), 3),
            "",
        )
