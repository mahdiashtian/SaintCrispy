"""YouTube response latency and inspection ownership without Telegram RPCs."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors

from bot.state.menus import MenuStore
from downloader_bot.downloaders.youtube.handler import handle_youtube_link
from schemas.media import DownloadError, Media, Quality

QUALITY = Quality(
    "public_video_360p_h264_mp4",
    "360p",
    "h264",
    None,
    "mp4",
    "video/mp4",
    "progressive",
    "public:mp4:360",
)
MEDIA = Media(
    "youtube",
    "jNQXAC9IVRw",
    "Title",
    "Author",
    19,
    "https://youtu.be/jNQXAC9IVRw",
    "https://thumbnail.example/slow.jpg",
    (QUALITY,),
)


def event(respond):
    return SimpleNamespace(
        raw_text=MEDIA.page_url, sender_id=7, chat_id=-10, id=42, respond=respond
    )


async def test_fast_cached_inspection_sends_one_usable_text_menu_without_thumbnail_fetch():
    respond = AsyncMock()
    menus = MenuStore()
    await handle_youtube_link(
        event(respond), SimpleNamespace(inspect=AsyncMock(return_value=MEDIA)), menus
    )
    respond.assert_awaited_once()
    options = respond.return_value.edit.call_args.kwargs
    assert "file" not in options and options["link_preview"] is False
    assert respond.call_args.kwargs["reply_to"] == 42
    button = options["buttons"][0][0]
    assert (await menus.fetch(button.type.data.decode().split(":")[1], 7, -10)).media == MEDIA


async def test_slow_inspection_acknowledges_before_menu_and_edits_same_reply():
    accepted, release = asyncio.Event(), asyncio.Event()
    status = SimpleNamespace(edit=AsyncMock())
    timestamps = []

    async def inspect(url):
        await release.wait()
        return MEDIA

    async def respond(text, **options):
        timestamps.append(time.monotonic())
        assert options["reply_to"] == 42 and "buttons" not in options
        accepted.set()
        return status

    started = time.monotonic()
    task = asyncio.create_task(
        handle_youtube_link(event(respond), SimpleNamespace(inspect=inspect), MenuStore())
    )
    try:
        async with asyncio.timeout(1):
            await accepted.wait()
        assert timestamps[0] - started < 0.5 and not task.done()
        release.set()
        await task
        assert len(timestamps) == 1
        status.edit.assert_awaited_once()
        assert status.edit.call_args.kwargs["buttons"]
        assert status.edit.call_args.kwargs["link_preview"] is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_failure_after_ack_replaces_pending_text_and_never_creates_quality_menu():
    acknowledged = asyncio.Event()
    status = SimpleNamespace(edit=AsyncMock())

    async def inspect(url):
        await acknowledged.wait()
        raise DownloadError("Safe failure", code="youtube_public_unavailable")

    async def respond(*args, **options):
        acknowledged.set()
        return status

    menus = MenuStore()
    await handle_youtube_link(event(respond), SimpleNamespace(inspect=inspect), menus)
    assert not menus._menus
    assert "Safe failure" in status.edit.call_args.args[0]
    assert status.edit.call_args.kwargs["buttons"] is None


async def test_cancellation_closes_inspection_and_removes_pending_ack():
    entered, closed = asyncio.Event(), asyncio.Event()
    status = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())

    async def inspect(url):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()

    task = asyncio.create_task(
        handle_youtube_link(
            event(AsyncMock(return_value=status)), SimpleNamespace(inspect=inspect), MenuStore()
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    status.delete.assert_awaited_once()


async def test_rejected_ack_does_not_start_origin_work():
    service = SimpleNamespace(inspect=AsyncMock())
    with pytest.raises(errors.ChatWriteForbiddenError):
        await handle_youtube_link(
            event(AsyncMock(side_effect=errors.ChatWriteForbiddenError(None))), service, MenuStore()
        )
    service.inspect.assert_not_awaited()


async def test_ack_timeout_does_not_cancel_the_inspection_or_hide_the_menu():
    acknowledged = asyncio.Event()
    responses = []

    async def inspect(url):
        await acknowledged.wait()
        return MEDIA

    async def respond(text, **options):
        if not acknowledged.is_set():
            acknowledged.set()
            raise TimeoutError
        responses.append(options)

    await handle_youtube_link(event(respond), SimpleNamespace(inspect=inspect), MenuStore())
    assert len(responses) == 1 and responses[0]["buttons"]
    assert responses[0]["reply_to"] == 42


async def test_deleted_ack_message_falls_back_to_a_new_menu_reply():
    acknowledged = asyncio.Event()
    status = SimpleNamespace(edit=AsyncMock(side_effect=errors.MessageIdInvalidError(None)))
    responses = []

    async def inspect(url):
        await acknowledged.wait()
        return MEDIA

    async def respond(text, **options):
        responses.append(options)
        acknowledged.set()
        return status

    await handle_youtube_link(event(respond), SimpleNamespace(inspect=inspect), MenuStore())
    assert len(responses) == 2
    assert responses[-1]["buttons"] and responses[-1]["reply_to"] == 42
