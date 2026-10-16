"""Quality buttons belong to the original sender and chat for every provider."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.handlers.quality import handle_quality
from bot.state.menus import MenuStore, PersistentMenuStore
from repositories.metadata import public_media
from schemas.media import DownloadError, Media, Quality
from services.access import ProviderAccess

SITES = {
    "sc": "soundcloud",
    "yt": "youtube",
    "ig": "instagram",
    "pin": "pinterest",
    "tt": "tiktok",
    "xv": "xvideos",
    "xnxx": "xnxx",
}
CHAT = -1000000000077


def callback(user, token, prefix, chat=CHAT):
    return SimpleNamespace(
        sender_id=user,
        chat_id=chat,
        data=f"{prefix}:{token}:0".encode(),
        pattern_match=(None, token.encode(), b"0"),
        answer=AsyncMock(),
        respond=AsyncMock(return_value=None),
        get_input_chat=AsyncMock(return_value=chat),
    )


@pytest.mark.parametrize("prefix,site", SITES.items())
@pytest.mark.parametrize("storage", ["memory", "persistent_hot", "persistent_cold"])
async def test_other_senders_including_main_admin_cannot_use_quality_buttons(prefix, site, storage):
    owner = 7
    quality = Quality("high", "High", "mp4", None, "mp4", "video/mp4", "progressive", "")
    media = Media(site, "id", "Title", "Author", 3, "https://example.com", None, (quality,))
    rows = {}

    async def save(token, user, chat, value, message_id=None):
        rows[token, user, chat] = {
            "metadata": json.dumps(public_media(value)),
            "message_id": message_id,
        }

    async def get(token, user, chat):
        return rows.get((token, user, chat))

    repository = SimpleNamespace(
        save_menu=AsyncMock(side_effect=save), get_menu=AsyncMock(side_effect=get)
    )
    menus = MenuStore() if storage == "memory" else PersistentMenuStore(repository)
    token = await menus.create(owner, CHAT, media, message_id=42)
    if storage == "persistent_cold":
        menus = PersistentMenuStore(repository)
    service = SimpleNamespace(deliver=AsyncMock())
    jobs = SimpleNamespace(reserve=Mock())
    limits = SimpleNamespace(check=AsyncMock())
    conversations = SimpleNamespace(set=AsyncMock(), finish_transfer=AsyncMock())
    policy = SimpleNamespace(check_download=AsyncMock())
    for intruder, chat in ((8, CHAT), (999, CHAT), (owner, CHAT - 1)):
        event = callback(intruder, token, prefix, chat)
        await handle_quality(
            event,
            service,
            menus,
            jobs,
            limits,
            conversations,
            access=ProviderAccess(intruder),
            policy=policy,
        )
        assert event.answer.await_count == 1 and event.answer.call_args.kwargs["alert"] is True
        assert "دکمه" in event.answer.call_args.args[0]
        event.respond.assert_not_awaited()
    service.deliver.assert_not_awaited()
    jobs.reserve.assert_not_called()
    limits.check.assert_not_awaited()
    conversations.set.assert_not_awaited()
    conversations.finish_transfer.assert_not_awaited()
    policy.check_download.assert_not_awaited()
    if storage == "persistent_hot":
        repository.get_menu.assert_not_awaited()

    # Rejected clicks leave the original menu usable by its owner.
    event = callback(owner, token, prefix)
    await handle_quality(event, service, menus, access=ProviderAccess(owner))
    service.deliver.assert_awaited_once()
    assert event.respond.call_args.kwargs["reply_to"] == 42


@pytest.mark.parametrize("expired", [False, True])
async def test_known_foreign_menu_is_rejected_without_sql_even_during_outage(expired):
    repository = SimpleNamespace(save_menu=AsyncMock(), get_menu=AsyncMock(side_effect=OSError()))
    store = PersistentMenuStore(repository)
    token = await store.create(7, CHAT, None)
    if expired:
        from dataclasses import replace

        store._menus[token] = replace(store._menus[token], expires_at=0)
    with pytest.raises(DownloadError, match="فرستنده") as failure:
        await store.fetch(token, 8, CHAT)
    assert failure.value.code == "menu_not_owned"
    repository.get_menu.assert_not_awaited()


async def test_concurrent_foreign_clicks_do_not_consume_or_change_the_owners_menu():
    store = MenuStore()
    token = await store.create(7, CHAT, None, message_id=42)
    original = store.get(token, 7, CHAT)
    service = SimpleNamespace(deliver=AsyncMock())
    events = [callback(user, token, "ig") for user in range(100, 200)]
    await asyncio.gather(*(handle_quality(event, service, store) for event in events))
    assert all(event.answer.call_args.kwargs["alert"] for event in events)
    assert store.get(token, 7, CHAT) is original
    service.deliver.assert_not_awaited()
