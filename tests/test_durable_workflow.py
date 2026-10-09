"""Exercise restart recovery, ownership, state races and first-message downloads."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from telethon import errors, types

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.handlers import register_handlers
from downloader_bot.bot.handlers.start import handle_start
from downloader_bot.bot.jobs.transfers import TransferJobs
from downloader_bot.bot.presentation import BotPresentation
from downloader_bot.bot.state.manager import ConversationManager
from downloader_bot.bot.state.menus import PersistentMenuStore
from downloader_bot.bot.state.states import ConversationState
from downloader_bot.core.request_context import user_request
from downloader_bot.repositories.postgres.workflow import public_media
from downloader_bot.schemas.media import DownloadError, Media, Quality, Source, TelegramFile
from downloader_bot.services.download import DownloadService
from downloader_bot.services.limits import RequestLimiter
from downloader_bot.services.user import UserService

QUALITY = Quality(
    "high",
    "High",
    "mp4",
    None,
    "mp4",
    "video/mp4",
    "progressive",
    "https://cdn.example/file?secret=SIGNED-SECRET",
)
MEDIA = Media(
    "youtube",
    "id",
    "Title",
    "Author",
    3,
    "https://www.youtube.com/watch?v=id",
    None,
    (QUALITY,),
    "AUTH-SECRET",
    "account",
)


class WorkflowMemory:
    def __init__(self):
        self.menus = {}
        self.states = {}
        self.users = {}

    async def touch_user(self, user_id, *, started=False):
        self.users[user_id] = self.users.get(user_id, False) or started

    async def save_menu(self, token, owner_id, chat_id, media, message_id=None):
        self.menus[token, owner_id, chat_id] = {
            "metadata": json.dumps(public_media(media)),
            "message_id": message_id,
        }

    async def get_menu(self, token, owner_id, chat_id):
        return self.menus.get((token, owner_id, chat_id))

    async def get_state(self, user_id, chat_id):
        return self.states.get((user_id, chat_id))

    async def set_state(self, user_id, chat_id, state, data):
        self.states[user_id, chat_id] = {"state": state, "data": data}

    async def finish_transfer(self, user_id, chat_id, transfer_id, state):
        old = self.states.get((user_id, chat_id))
        if old and old["data"].get("transfer_id") == transfer_id:
            await self.set_state(user_id, chat_id, state, {})

    async def finish_inspection(self, user_id, chat_id, request_id):
        old = self.states.get((user_id, chat_id))
        if old and old["state"] == "INSPECTING" and old["data"].get("request_id") == request_id:
            await self.set_state(user_id, chat_id, "FAILED", {})


async def test_restart_restores_owned_menu_without_persisting_source_secrets():
    repo = WorkflowMemory()
    original = PersistentMenuStore(repo, limit=1)
    token = await original.create(10, 20, MEDIA, message_id=30)
    await original.create(11, 21, MEDIA)
    restarted = PersistentMenuStore(repo)
    restored = await restarted.fetch(token, 10, 20)
    assert restored.message_id == 30
    assert restored.media.requires_refresh
    assert restored.media.account_id == "account"
    assert restored.media.authorization is None
    assert restored.media.qualities[0].endpoint == ""
    raw = repo.menus[token, 10, 20]["metadata"]
    assert "AUTH-SECRET" not in raw and "SIGNED-SECRET" not in raw
    with pytest.raises(DownloadError):
        await restarted.fetch(token, 11, 20)
    with pytest.raises(DownloadError):
        await restarted.fetch(token, 10, 21)


async def test_stale_transfer_completion_cannot_replace_new_conversation():
    repo = WorkflowMemory()
    manager = ConversationManager(repo)
    await manager.set(1, 2, ConversationState.TRANSFERRING, transfer_id="old")
    await manager.set(1, 2, ConversationState.CHOOSING_QUALITY, menu_token="new")
    await manager.finish_transfer(1, 2, "old", ConversationState.DONE)
    assert await manager.get(1, 2) == (ConversationState.CHOOSING_QUALITY, {"menu_token": "new"})
    await manager.set(1, 2, ConversationState.TRANSFERRING, transfer_id="current")
    await manager.finish_transfer(1, 2, "current", ConversationState.CANCELLED)
    assert await manager.get(1, 2) == (ConversationState.CANCELLED, {})


async def test_invalid_persisted_state_recovers_to_main():
    repo = WorkflowMemory()
    repo.states[1, 2] = {"state": "OLD_STATE", "data": {}}
    manager = ConversationManager(repo)
    assert await manager.get(1, 2) == (ConversationState.MAIN, {})
    assert repo.states[1, 2]["state"] == "MAIN"


@pytest.mark.parametrize(
    ("site", "url"),
    [
        ("soundcloud", "https://soundcloud.com/user/track"),
        ("youtube", "https://www.youtube.com/watch?v=l6zflbTGNFQ"),
        ("instagram", "https://www.instagram.com/reel/Chunk8-jurw/"),
        ("pinterest", "https://www.pinterest.com/pin/123456789/"),
        ("xvideos", "https://www.xvideos.com/video.demo/title"),
        ("xnxx", "https://www.xnxx.com/video-demo/title"),
    ],
)
async def test_first_link_without_start_reaches_quality_and_delivery(site, url):
    repo = WorkflowMemory()
    manager = ConversationManager(repo)
    menus = PersistentMenuStore(repo, conversations=manager)
    registrations, responses, delivered = [], [], []
    media = replace(MEDIA, site=site, page_url=url)

    async def inspect(value):
        assert value == url
        return media

    async def deliver(peer, selected, quality, progress):
        delivered.append((selected.content_id, quality.key))
        progress.phase = "done"

    async def respond(text, **options):
        responses.append((text, options))
        return SimpleNamespace(edit=respond)

    async def answer(*args, **options):
        pass

    async def peer():
        return types.InputPeerUser(10, 123)

    client = SimpleNamespace(
        add_event_handler=lambda callback, builder: registrations.append((callback, builder))
    )
    async with TransferJobs() as jobs:
        register_handlers(
            client,
            SimpleNamespace(inspect=inspect, deliver=deliver),
            menus,
            jobs,
            RequestLimiter(),
            conversations=manager,
            users=UserService(repo),
            bot_username="ActualBot",
            presentation=BotPresentation(),
        )
        event = SimpleNamespace(
            raw_text=url,
            sender_id=10,
            chat_id=10,
            id=77,
            respond=respond,
            answer=answer,
            get_input_chat=peer,
        )
        callback = next(
            cb
            for cb, builder in registrations
            if hasattr(builder, "pattern") and builder.pattern and builder.pattern(url)
        )
        await callback(event)
        assert repo.users == {10: False}
        assert (await manager.get(10, 10))[0] == ConversationState.CHOOSING_QUALITY
        assert responses[0][1]["reply_to"] == 77
        data = responses[0][1]["buttons"][0][0].type.data
        handler, builder = next(
            (cb, b) for cb, b in registrations if hasattr(b, "match") and b.match(data)
        )
        event.pattern_match = builder.match(data)
        await handler(event)
        await jobs.join()
    assert delivered == [("id", "high")]
    assert (await manager.get(10, 10))[0] == ConversationState.DONE
    assert responses[1][1]["reply_to"] == 77


async def test_restored_menu_refreshes_before_upload_and_checks_content_identity():
    stored = replace(
        MEDIA, authorization=None, requires_refresh=True, qualities=(replace(QUALITY, endpoint=""),)
    )
    resolved, sent = [], []

    async def inspect(url):
        return MEDIA

    async def resolve(media, quality):
        assert media.authorization == "AUTH-SECRET" and quality.endpoint
        resolved.append(1)
        return Source("source", "progressive")

    async def new_file(*args):
        sent.append(1)
        return TelegramFile(1, 2, b"ref", b"peer", 3)

    async def save(*args):
        pass

    provider = SimpleNamespace(inspect=inspect, resolve=resolve)
    service = DownloadService(
        provider, SimpleNamespace(save=save), SimpleNamespace(new_file=new_file)
    )
    await service._transfer("peer", stored, stored.qualities[0], None)
    assert resolved == sent == [1]

    async def replacement(url):
        return replace(MEDIA, content_id="different")

    provider.inspect = replacement
    with pytest.raises(DownloadError):
        await service._transfer("peer", stored, stored.qualities[0], None)
    assert sent == [1]


async def test_download_reply_is_only_attached_in_original_chat():
    calls = []

    async def send_file(peer, media, **options):
        calls.append(options)

    delivery = TelegramDelivery(SimpleNamespace(send_file=send_file), None, "ffmpeg")
    with user_request(10, 10, "request", 77):
        await delivery._send_file(types.InputPeerUser(10, 123), "file")
        await delivery._send_file(types.InputPeerChannel(20, 456), "storage-file")
    assert calls[0]["reply_to"] == 77
    assert "reply_to" not in calls[1]


def test_custom_emoji_uses_telegram_utf16_offsets():
    presentation = BotPresentation()
    presentation.emojis = {"🎵": 101, "✅": 102}
    text = "📬 موزیک 🎵 ✅"
    entities = presentation.entities(text)
    assert [entity.document_id for entity in entities] == [101, 102]
    assert [entity.length for entity in entities] == [2, 1]
    assert [entity.offset for entity in entities] == [
        len(text[: text.index(e)].encode("utf-16-le")) // 2 for e in ("🎵", "✅")
    ]


async def test_custom_emoji_permission_failure_falls_back_and_does_not_repeat():
    presentation = BotPresentation()
    presentation.emojis = {"🎵": 101}
    calls = []

    async def respond(text, **options):
        calls.append(options)
        if "formatting_entities" in options:
            raise errors.PremiumAccountRequiredError(request=None)
        return "sent"

    event = SimpleNamespace(respond=respond, id=77)
    assert await presentation.respond(event, "🎵 تست") == "sent"
    assert await presentation.respond(event, "🎵 تست") == "sent"
    assert len(calls) == 3 and "formatting_entities" not in calls[-1]
    assert all(options["reply_to"] == 77 for options in calls)


async def test_start_uses_actual_username_and_does_not_invent_provider_bots():
    repo = WorkflowMemory()
    responses = []

    async def respond(text, **options):
        responses.append((text, options))

    event = SimpleNamespace(sender_id=10, chat_id=20, id=77, respond=respond)
    await handle_start(
        event,
        bot_username="ActualBot",
        users=UserService(repo),
        conversations=ConversationManager(repo),
        presentation=BotPresentation(),
    )
    assert repo.users == {10: True}
    text, options = responses[0]
    assert "@ActualBot" in text and "/start" in text
    assert text.count("@") == 2
    assert all(
        name in text
        for name in ("SoundCloud", "YouTube", "Instagram", "Pinterest", "XVideos", "XNXX")
    )
    assert options["reply_to"] == 77
