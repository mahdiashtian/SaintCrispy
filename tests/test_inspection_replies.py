"""Request-owned loading/menu messages and original-link publication, without RPCs."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telethon import errors

from bot.premium_emoji import BotPresentation, PresentedEvent
from bot.state.menus import MenuStore
from bot.texts.downloads import inspection_text, quality_menu_text
from downloader_bot.downloaders.instagram.handler import handle_instagram_link
from downloader_bot.downloaders.pinterest.handler import handle_pinterest_link
from downloader_bot.downloaders.soundcloud.handler import handle_soundcloud_link
from downloader_bot.downloaders.xnxx.handler import handle_xnxx_link
from downloader_bot.downloaders.xvideos.handler import handle_xvideos_link
from downloader_bot.downloaders.youtube.handler import handle_youtube_link
from schemas.media import DownloadError, Media, Quality

PROVIDERS = [
    ("youtube", "yt", handle_youtube_link, "https://youtu.be/jNQXAC9IVRw"),
    ("instagram", "ig", handle_instagram_link, "https://www.instagram.com/reel/DeR9eU8idi0/"),
    ("soundcloud", "sc", handle_soundcloud_link, "https://soundcloud.com/artist/track"),
    ("pinterest", "pin", handle_pinterest_link, "https://www.pinterest.com/pin/123456789/"),
    ("xvideos", "xv", handle_xvideos_link, "https://www.xvideos.com/video.demo/example"),
    ("xnxx", "xnxx", handle_xnxx_link, "https://www.xnxx.com/video-demo/example"),
]
QUALITY = Quality("720", "720p", "h264", None, "mp4", "video/mp4", "progressive", "")


def media_for(site, url):
    return Media(site, "identity", "Title", "Author", 10, url, None, (QUALITY,))


def incoming(url, chat_id=7, message_id=42):
    status = SimpleNamespace(id=100, edit=AsyncMock(), delete=AsyncMock())
    event = SimpleNamespace(
        raw_text=url,
        sender_id=7,
        chat_id=chat_id,
        id=message_id,
        respond=AsyncMock(return_value=status),
    )
    return event, status


@pytest.mark.parametrize("site,prefix,handler,url", PROVIDERS)
@pytest.mark.parametrize("chat_id", [7, -1000000000123])
@pytest.mark.parametrize("cached", [False, True])
async def test_loading_precedes_inspection_and_same_message_becomes_menu(
    site, prefix, handler, url, chat_id, cached
):
    event, status = incoming(url, chat_id)
    entered, release = asyncio.Event(), asyncio.Event()
    media = media_for(site, url)

    async def inspect(actual):
        assert actual == url
        event.respond.assert_awaited_once_with(
            inspection_text(site), parse_mode=None, reply_to=42, link_preview=False
        )
        entered.set()
        if not cached:
            await release.wait()
        return media

    menus = MenuStore()
    task = asyncio.create_task(handler(event, SimpleNamespace(inspect=inspect), menus))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if not cached:
            assert not task.done() and not menus._menus
            status.edit.assert_not_awaited()
        release.set()
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    event.respond.assert_awaited_once()
    status.edit.assert_awaited_once()
    assert status.edit.call_args.args[0] == quality_menu_text(media)
    options = status.edit.call_args.kwargs
    assert options["link_preview"] is False and "file" not in options
    assert "reply_to" not in options
    data = options["buttons"][0][0].type.data.decode().split(":")
    assert data[0] == prefix and data[2] == "0"
    menu = await menus.fetch(data[1], 7, chat_id)
    assert menu.media is media and menu.message_id == 42
    assert menu.message_id != status.id
    status.delete.assert_not_awaited()


@pytest.mark.parametrize("site,prefix,handler,url", PROVIDERS)
@pytest.mark.parametrize(
    "failure", [DownloadError("Safe failure"), httpx.ConnectError("network"), TimeoutError()]
)
async def test_inspection_failure_replaces_loading_without_creating_another_reply(
    site, prefix, handler, url, failure
):
    event, status = incoming(url)
    menus = MenuStore()
    await handler(event, SimpleNamespace(inspect=AsyncMock(side_effect=failure)), menus)
    event.respond.assert_awaited_once()
    status.edit.assert_awaited_once()
    assert status.edit.call_args.kwargs["buttons"] is None
    assert "⚠" in status.edit.call_args.args[0]
    assert not menus._menus
    status.delete.assert_not_awaited()


@pytest.mark.parametrize("site,prefix,handler,url", PROVIDERS)
async def test_cancelled_or_failed_storage_removes_unfinished_loading(site, prefix, handler, url):
    for storage_failure in (False, True):
        event, status = incoming(url)
        entered = asyncio.Event()

        async def inspect(actual):
            entered.set()
            if not storage_failure:
                await asyncio.Future()
            return media_for(site, url)

        menus = SimpleNamespace(create=AsyncMock(side_effect=OSError("database unavailable")))
        task = asyncio.create_task(handler(event, SimpleNamespace(inspect=inspect), menus))
        await asyncio.wait_for(entered.wait(), 1)
        if storage_failure:
            with pytest.raises(OSError):
                await task
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        status.delete.assert_awaited_once()
        status.edit.assert_not_awaited()


@pytest.mark.parametrize("site,prefix,handler,url", PROVIDERS)
async def test_deleted_loading_falls_back_to_menu_reply_on_original_link(
    site, prefix, handler, url
):
    event, status = incoming(url)
    status.edit.side_effect = errors.MessageIdInvalidError(None)
    await handler(
        event, SimpleNamespace(inspect=AsyncMock(return_value=media_for(site, url))), MenuStore()
    )
    assert event.respond.await_count == 2
    assert event.respond.call_args.kwargs["reply_to"] == 42
    assert event.respond.call_args.kwargs["buttons"]


@pytest.mark.parametrize("site,prefix,handler,url", PROVIDERS)
async def test_equal_concurrent_links_keep_independent_loading_and_reply_identity(
    site, prefix, handler, url
):
    events = [incoming(url, message_id=identity) for identity in (42, 43)]
    service = SimpleNamespace(inspect=AsyncMock(return_value=media_for(site, url)))
    menus = MenuStore()
    await asyncio.gather(*(handler(event, service, menus) for event, _ in events))
    tokens = []
    for event, status in events:
        event.respond.assert_awaited_once()
        status.edit.assert_awaited_once()
        token = status.edit.call_args.kwargs["buttons"][0][0].type.data.decode().split(":")[1]
        assert (await menus.fetch(token, 7, 7)).message_id == event.id
        tokens.append(token)
    assert tokens[0] != tokens[1]


async def test_album_edits_loading_for_first_page_and_preserves_all_remaining_buttons():
    site, prefix, handler, url = PROVIDERS[1]
    event, status = incoming(url)
    media = replace(
        media_for(site, url), qualities=tuple(replace(QUALITY, key=str(i)) for i in range(120))
    )
    await handler(event, SimpleNamespace(inspect=AsyncMock(return_value=media)), MenuStore())
    assert event.respond.await_count == 3
    status.edit.assert_awaited_once()
    pages = [status.edit.call_args.kwargs["buttons"]]
    pages.extend(call.kwargs["buttons"] for call in event.respond.await_args_list[1:])
    data = [button.type.data for page in pages for row in page for button in row]
    assert [int(value.rsplit(b":", 1)[1]) for value in data] == list(range(120))
    assert all(call.kwargs["reply_to"] == 42 for call in event.respond.await_args_list)


async def test_loading_and_replacement_menu_keep_premium_emoji_formatting():
    site, prefix, handler, url = PROVIDERS[1]
    event, status = incoming(url)
    presentation = BotPresentation()
    presentation.emojis = {"📸": 101, "⏳": 102}
    await handler(
        PresentedEvent(event, presentation),
        SimpleNamespace(inspect=AsyncMock(return_value=media_for(site, url))),
        MenuStore(),
    )
    assert [e.document_id for e in event.respond.call_args.kwargs["formatting_entities"]] == [
        101,
        102,
    ]
    assert [e.document_id for e in status.edit.call_args.kwargs["formatting_entities"]] == [101]
