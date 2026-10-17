"""Public YouTube access retains the normal ownership and delivery rules."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers import register_handlers
from bot.handlers.quality import handle_quality
from bot.state.menus import MenuStore
from bot.texts import sites_text, start_text
from core.config import ConfigurationError, Settings
from core.request_context import user_request
from schemas.media import Media, Quality
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


@pytest.mark.parametrize("chat", [7, -100123])
@pytest.mark.parametrize("sender", [7, ADMIN])
async def test_every_user_can_create_youtube_menu_without_start_in_private_or_group(chat, sender):
    service = SimpleNamespace(inspect=AsyncMock(return_value=MEDIA))
    menus = MenuStore()
    handlers = registered(service, menus, access=ProviderAccess(ADMIN))
    callback = next(
        cb for cb, b in handlers if getattr(b, "pattern", None) and b.pattern(MEDIA.page_url)
    )
    event = SimpleNamespace(
        raw_text=MEDIA.page_url,
        sender_id=sender,
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
    assert menus.get(token, sender, chat).media == MEDIA


@pytest.mark.parametrize(
    "site,user", [("youtube", ADMIN), ("youtube", 7), ("soundcloud", 7), ("instagram", 7)]
)
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


@pytest.mark.parametrize("configured_admin", [None, ADMIN])
@pytest.mark.parametrize("user", [None, 7, ADMIN])
async def test_public_policy_and_service_allow_youtube_caches(configured_admin, user):
    policy = ProviderAccess(configured_admin)
    assert policy.allows("youtube", user)
    policy.require("youtube", user)
    repository = SimpleNamespace(record_delivery=AsyncMock())
    service = DownloadService(
        SimpleNamespace(site_for=lambda url: "youtube"), repository, None, access=policy
    )
    service._inspect_with_history = AsyncMock(return_value=MEDIA)
    service._deliver = AsyncMock(return_value="reused")
    with user_request(user, 7):
        assert await service.inspect(MEDIA.page_url) == MEDIA
        assert await service.deliver(7, MEDIA, QUALITY) == "reused"
    service._inspect_with_history.assert_awaited_once()
    service._deliver.assert_awaited_once()


@pytest.mark.parametrize("sender", [7, ADMIN])
async def test_start_and_sites_match_the_callers_access(sender):
    handlers = registered(None, None, access=ProviderAccess(ADMIN))
    callback = next(cb for cb, b in handlers if getattr(b, "pattern", None) and b.pattern("/start"))
    event = SimpleNamespace(
        raw_text="/start", sender_id=sender, chat_id=sender, id=42, respond=AsyncMock()
    )
    await callback(event)
    assert ("YouTube" in event.respond.call_args.args[0]) is True
    home, builder = next(
        (cb, b)
        for cb, b in handlers
        if getattr(b, "match", None) and b.match(f"home:{sender}:sites".encode())
    )
    event.pattern_match = builder.match(f"home:{sender}:sites".encode())
    event.answer, event.edit = AsyncMock(), AsyncMock()
    await home(event)
    assert ("YouTube" in event.edit.call_args.args[0]) is True
    assert "Instagram" in event.edit.call_args.args[0]


def test_public_text_defaults_include_youtube():
    assert "YouTube" in start_text(None)
    assert "YouTube" in sites_text()


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
