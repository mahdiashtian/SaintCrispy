"""Temporary YouTube access must precede admission, extraction and cached delivery."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.handlers import register_handlers
from bot.handlers.quality import handle_quality
from bot.state.menus import MenuStore
from bot.texts import sites_text, start_text
from core.config import ConfigurationError, Settings
from core.request_context import user_request
from schemas.media import DownloadError, Media, Quality
from services.access import ProviderAccess
from services.download import DownloadService

ADMIN = 987654321
QUALITY = Quality("720", "720p", "h264", None, "mp4", "video/mp4", "progressive", "")
MEDIA = Media(
    "youtube",
    "jNQXAC9IVRw",
    "Title",
    "Author",
    19,
    "https://youtu.be/jNQXAC9IVRw",
    None,
    (QUALITY,),
)


def registered(service, menus, *, access, limits=None, **kwargs):
    handlers = []
    register_handlers(
        SimpleNamespace(add_event_handler=lambda cb, builder: handlers.append((cb, builder))),
        service,
        menus,
        access=access,
        limits=limits,
        **kwargs,
    )
    return handlers


@pytest.mark.parametrize(
    "url",
    [
        MEDIA.page_url,
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://youtube.com/shorts/jNQXAC9IVRw",
        "https://music.youtube.com/watch?v=jNQXAC9IVRw",
        "https://www.youtube-nocookie.com/embed/jNQXAC9IVRw",
    ],
)
@pytest.mark.parametrize("chat", [7, -100123])
@pytest.mark.parametrize("main_admin", [None, ADMIN])
async def test_public_youtube_denied_before_any_quota_storage_or_inspection(url, chat, main_admin):
    service = SimpleNamespace(inspect=AsyncMock())
    menus = MenuStore()
    limits = SimpleNamespace(check_link=AsyncMock(side_effect=OSError("database unavailable")))
    users = SimpleNamespace(touch_user=AsyncMock())
    conversations = SimpleNamespace(set=AsyncMock(), finish_inspection=AsyncMock())
    handlers = registered(
        service,
        menus,
        access=ProviderAccess(main_admin),
        limits=limits,
        users=users,
        conversations=conversations,
    )
    callback = next(cb for cb, b in handlers if getattr(b, "pattern", None) and b.pattern(url))
    event = SimpleNamespace(
        raw_text=url,
        sender_id=7,
        chat_id=chat,
        id=42,
        respond=AsyncMock(),
        is_group=chat < 0,
        is_admin=True,
    )
    await callback(event)
    service.inspect.assert_not_awaited()
    limits.check_link.assert_not_awaited()
    users.touch_user.assert_not_awaited()
    conversations.set.assert_not_awaited()
    conversations.finish_inspection.assert_not_awaited()
    assert not menus._menus
    assert "غیرفعال" in event.respond.call_args.args[0]
    assert event.respond.call_args.kwargs["reply_to"] == 42


@pytest.mark.parametrize("chat", [ADMIN, -100123])
async def test_main_admin_can_create_youtube_menu_without_start_in_private_or_group(chat):
    service = SimpleNamespace(inspect=AsyncMock(return_value=MEDIA))
    menus = MenuStore()
    handlers = registered(service, menus, access=ProviderAccess(ADMIN))
    callback = next(
        cb for cb, b in handlers if getattr(b, "pattern", None) and b.pattern(MEDIA.page_url)
    )
    event = SimpleNamespace(
        raw_text=MEDIA.page_url,
        sender_id=ADMIN,
        chat_id=chat,
        id=42,
        respond=AsyncMock(),
        is_group=chat < 0,
    )
    await callback(event)
    service.inspect.assert_awaited_once_with(MEDIA.page_url)
    button = event.respond.return_value.edit.call_args.kwargs["buttons"][0][0]
    prefix, token, index = button.type.data.decode().split(":")
    assert prefix == "yt" and index == "0"
    assert menus.get(token, ADMIN, chat).media == MEDIA


async def test_old_youtube_callback_denied_even_while_menu_database_is_unavailable():
    menus = SimpleNamespace(fetch=AsyncMock(side_effect=OSError("database unavailable")))
    service = SimpleNamespace(deliver=AsyncMock())
    jobs = SimpleNamespace(reserve=Mock())
    event = SimpleNamespace(
        sender_id=7, chat_id=7, data=b"yt:0123456789abcdef:0", answer=AsyncMock()
    )
    await handle_quality(event, service, menus, jobs=jobs, access=ProviderAccess(ADMIN))
    menus.fetch.assert_not_awaited()
    service.deliver.assert_not_awaited()
    jobs.reserve.assert_not_called()
    assert event.answer.call_args.kwargs["alert"]


@pytest.mark.parametrize("prefix", [b"yt", b"sc", b"ig", b"pin", b"xnxx", b"xv"])
async def test_legacy_youtube_menu_cannot_bypass_access_by_changing_callback_prefix(prefix):
    menus = MenuStore()
    token = await menus.create(7, -100123, MEDIA, message_id=42)
    limits = SimpleNamespace(check=AsyncMock())
    jobs = SimpleNamespace(reserve=Mock())
    service = SimpleNamespace(deliver=AsyncMock())
    event = SimpleNamespace(
        sender_id=7,
        chat_id=-100123,
        data=prefix + b":" + token.encode() + b":0",
        pattern_match=(None, token.encode(), b"0"),
        answer=AsyncMock(),
    )
    await handle_quality(
        event, service, menus, jobs=jobs, limits=limits, access=ProviderAccess(ADMIN)
    )
    assert "غیرفعال" in event.answer.call_args.args[0]
    service.deliver.assert_not_awaited()
    jobs.reserve.assert_not_called()
    limits.check.assert_not_awaited()


@pytest.mark.parametrize("site,user", [("youtube", ADMIN), ("soundcloud", 7), ("instagram", 7)])
async def test_allowed_callbacks_keep_their_existing_delivery_path(site, user):
    media = Media(
        site,
        MEDIA.content_id,
        MEDIA.title,
        MEDIA.artist,
        MEDIA.duration,
        MEDIA.page_url,
        None,
        MEDIA.qualities,
    )
    menus = MenuStore()
    token = await menus.create(user, -100123, media, message_id=42)
    peer = object()

    async def deliver(actual_peer, actual_media, quality, progress):
        assert actual_peer is peer and actual_media == media and quality == QUALITY
        progress.phase = "done"

    service = SimpleNamespace(deliver=AsyncMock(side_effect=deliver))
    event = SimpleNamespace(
        sender_id=user,
        chat_id=-100123,
        pattern_match=(None, token.encode(), b"0"),
        answer=AsyncMock(),
        respond=AsyncMock(return_value=None),
        get_input_chat=AsyncMock(return_value=peer),
    )
    await handle_quality(event, service, menus, access=ProviderAccess(ADMIN))
    service.deliver.assert_awaited_once()
    assert event.respond.call_args.kwargs["reply_to"] == 42


async def test_service_blocks_shared_caches_and_missing_user_context_before_any_io():
    downloader = SimpleNamespace(site_for=lambda url: "youtube")
    repository = SimpleNamespace(record_delivery=AsyncMock())
    service = DownloadService(downloader, repository, None, access=ProviderAccess(ADMIN))
    service._inspect_with_history = AsyncMock(return_value=MEDIA)
    service._deliver = AsyncMock(return_value="reused")

    # The internal methods model warm metadata and Telegram-file caches.
    async def rejected():
        for user in (None, 7):
            with user_request(user, 7):
                with pytest.raises(DownloadError, match="غیرفعال"):
                    await service.inspect(MEDIA.page_url)
                with pytest.raises(DownloadError, match="غیرفعال"):
                    await service.deliver(7, MEDIA, QUALITY)

    await asyncio.gather(*(rejected() for _ in range(1000)))
    service._inspect_with_history.assert_not_awaited()
    service._deliver.assert_not_awaited()
    repository.record_delivery.assert_not_awaited()
    with user_request(ADMIN, -100123):
        assert await service.inspect(MEDIA.page_url) == MEDIA
        assert await service.deliver(-100123, MEDIA, QUALITY) == "reused"
    service._deliver.assert_awaited_once()
    repository.record_delivery.assert_awaited_once_with(ADMIN, MEDIA, QUALITY, "reused")


@pytest.mark.parametrize("sender", [7, ADMIN])
async def test_start_and_sites_match_the_callers_access(sender):
    handlers = registered(None, None, access=ProviderAccess(ADMIN))
    callback = next(cb for cb, b in handlers if getattr(b, "pattern", None) and b.pattern("/start"))
    event = SimpleNamespace(
        raw_text="/start", sender_id=sender, chat_id=sender, id=42, respond=AsyncMock()
    )
    await callback(event)
    assert ("YouTube" in event.respond.call_args.args[0]) == (sender == ADMIN)
    home, builder = next(
        (cb, b)
        for cb, b in handlers
        if getattr(b, "match", None) and b.match(f"home:{sender}:sites".encode())
    )
    event.pattern_match = builder.match(f"home:{sender}:sites".encode())
    event.answer, event.edit = AsyncMock(), AsyncMock()
    await home(event)
    assert ("YouTube" in event.edit.call_args.args[0]) == (sender == ADMIN)
    assert "Instagram" in event.edit.call_args.args[0]


def test_public_text_defaults_hide_youtube():
    assert "YouTube" not in start_text(None)
    assert "YouTube" not in sites_text()


@pytest.mark.parametrize("value", [None, "", "  ", str(ADMIN), "  " + str(ADMIN) + "  "])
def test_main_admin_environment_is_optional_and_numeric(monkeypatch, value):
    env = {"API_ID": "1", "API_HASH": "test", "BOT_TOKEN": "1:test", "DATABASE_URL": "test"}
    if value is not None:
        env["MAIN_ADMIN_ID"] = value
    monkeypatch.setattr(os, "environ", env)
    assert Settings.from_environment().main_admin_id == (ADMIN if value and value.strip() else None)


@pytest.mark.parametrize("value", ["0", "-1", "@admin", "1,2", str(2**63)])
def test_invalid_main_admin_setting_fails_without_granting_access(monkeypatch, value):
    monkeypatch.setattr(
        os,
        "environ",
        {
            "API_ID": "1",
            "API_HASH": "test",
            "BOT_TOKEN": "1:test",
            "DATABASE_URL": "test",
            "MAIN_ADMIN_ID": value,
        },
    )
    with pytest.raises(ConfigurationError) as failure:
        Settings.from_environment()
    assert failure.value.fields == ("MAIN_ADMIN_ID",)


@pytest.mark.parametrize("value", [0, -1, True, "7"])
def test_access_policy_rejects_invalid_admin_identity(value):
    with pytest.raises(ValueError):
        ProviderAccess(value)
