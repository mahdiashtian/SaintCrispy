"""Telegram-shaped group updates and reply/menu/delivery routing, without RPCs."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, types, utils

from bot.checks.group import group_kind, human_id, participant_standing
from bot.delivery import TelegramDelivery
from bot.handlers import register_handlers
from bot.handlers.dispatcher import Dispatcher
from bot.handlers.groups import GroupTracker
from bot.handlers.limited import LimitedHandlers
from bot.handlers.quality import handle_quality
from bot.handlers.start import handle_start
from bot.state.menus import MenuStore
from core.config import ConfigurationError, Settings
from core.request_context import request_chat, request_message, user_request
from schemas.group import GroupChange
from schemas.media import Media, Quality
from services.access import ProviderAccess
from services.limits import RequestLimiter

NOW = datetime(2026, 10, 15, 14, tzinfo=UTC)
BOT_ID = 123
CHAT_ID = -1000000000077
PEER = types.InputPeerChannel(77, 999)


def channel(**values):
    return types.Channel(
        id=77, title="Group", photo=types.ChatPhotoEmpty(), date=NOW, megagroup=True, **values
    )


def group_service():
    return SimpleNamespace(
        record=AsyncMock(),
        get=AsyncMock(return_value=None),
        migrate=AsyncMock(),
        claim_link=AsyncMock(return_value=0),
    )


@pytest.mark.parametrize("kind", ["group", "supergroup"])
async def test_own_join_promotion_demotion_and_removal_updates(kind):
    service, entity = group_service(), channel()
    if kind == "group":
        entity = types.Chat(77, "Group", types.ChatPhotoEmpty(), 5, NOW, 1)
        updates = [
            types.UpdateChatParticipantAdd(77, BOT_ID, 50, NOW, 1),
            types.UpdateChatParticipantAdmin(77, BOT_ID, True, 2),
            types.UpdateChatParticipantAdmin(77, BOT_ID, False, 3),
            types.UpdateChatParticipantDelete(77, BOT_ID, 4),
        ]
    else:
        member = types.ChannelParticipant(BOT_ID, NOW)
        admin = types.ChannelParticipantAdmin(BOT_ID, 50, NOW, types.ChatAdminRights())
        updates = [
            types.UpdateChannelParticipant(
                77, NOW, 50, BOT_ID, index, prev_participant=old, new_participant=new
            )
            for index, (old, new) in enumerate(
                ((None, member), (member, admin), (admin, member), (member, None)), 1
            )
        ]
    tracker = GroupTracker(
        SimpleNamespace(get_entity=AsyncMock(return_value=entity)), service, BOT_ID
    )
    for update in updates:
        await tracker.handle_update(update)
    changes = [call.args[0] for call in service.record.call_args_list]
    assert [item.kind for item in changes] == ["added", "promoted", "demoted", "removed"]
    assert [item.admin for item in changes] == (
        [None, True, False, False] if kind == "group" else [False, True, False, False]
    )
    assert all(item.chat_kind == kind and item.chat_id < 0 for item in changes)
    assert len({item.event_key for item in changes}) == 4
    assert changes[0].actor_id == 50


async def test_raw_service_add_remove_and_migration_are_received_without_a_sender_gate():
    callbacks, service = [], group_service()
    client = SimpleNamespace(
        add_event_handler=lambda cb, ev: callbacks.append((cb, ev)),
        get_entity=AsyncMock(return_value=None),
    )
    register_handlers(client, None, None, groups=service, bot_id=BOT_ID)
    raw = next(cb for cb, _ in callbacks if getattr(cb.func, "__name__", "") == "handle_update")
    for index, action in enumerate(
        (
            types.MessageActionChatAddUser([BOT_ID]),
            types.MessageActionChatDeleteUser(BOT_ID),
            types.MessageActionChatMigrateTo(77),
        ),
        1,
    ):
        message = types.MessageService(
            id=index,
            peer_id=types.PeerChat(10),
            date=NOW,
            action=action,
            from_id=types.PeerUser(50),
        )
        await raw(types.UpdateNewMessage(message, index, 1))
    assert [call.args[0].kind for call in service.record.call_args_list] == ["added", "removed"]
    service.migrate.assert_awaited_once_with(-10, CHAT_ID, NOW)


async def test_other_members_broadcast_channels_and_unknown_peers_cannot_create_groups():
    service = group_service()
    client = SimpleNamespace(get_entity=AsyncMock(return_value=channel(broadcast=True)))
    client.get_entity.return_value.megagroup = False
    tracker = GroupTracker(client, service, BOT_ID)
    await tracker.handle_update(types.UpdateChatParticipantAdd(77, 555, 1, NOW, 1))
    await tracker.handle_update(
        types.UpdateChannelParticipant(
            77, NOW, 1, BOT_ID, 2, new_participant=types.ChannelParticipant(BOT_ID, NOW)
        )
    )
    assert service.record.await_count == 0
    client.get_entity.side_effect = ValueError("Unknown input entity")
    await tracker.handle_update(
        types.UpdateChannelParticipant(
            77, NOW, 1, BOT_ID, 3, new_participant=types.ChannelParticipant(BOT_ID, NOW)
        )
    )
    assert service.record.await_count == 0


async def test_update_entity_used_without_network_and_restriction_is_not_removal():
    service = group_service()
    client = SimpleNamespace(get_entity=AsyncMock(side_effect=AssertionError("No RPC needed")))
    tracker = GroupTracker(client, service, BOT_ID)
    restricted = types.ChannelParticipantBanned(
        types.PeerUser(BOT_ID), 50, NOW, types.ChatBannedRights(NOW, send_messages=True)
    )
    update = types.UpdateChannelParticipant(77, NOW, 50, BOT_ID, 1, new_participant=restricted)
    update._entities = {CHAT_ID: channel()}
    await tracker.handle_update(update)
    change = service.record.call_args.args[0]
    assert change.active and not change.admin and change.can_send is False
    assert change.kind != "removed"


async def test_unknown_permission_probe_does_not_invent_removal_or_demotion(caplog):
    service = group_service()
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=channel()),
        get_permissions=AsyncMock(side_effect=errors.ChannelPrivateError(None)),
    )
    await GroupTracker(client, service, BOT_ID).handle_update(types.UpdateChannel(77))
    assert service.record.await_count == 0
    assert "group_standing_unknown" in caplog.text


async def test_definite_self_permission_snapshot_refreshes_standing():
    service = group_service()
    admin = types.ChannelParticipantAdmin(BOT_ID, 50, NOW, types.ChatAdminRights())
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=channel()),
        get_permissions=AsyncMock(return_value=SimpleNamespace(participant=admin)),
    )
    await GroupTracker(client, service, BOT_ID).handle_update(types.UpdateChannel(77))
    change = service.record.call_args.args[0]
    assert change.active and change.admin and change.can_send


async def test_thousand_message_observations_coalesce_and_keep_unknown_admin_unknown():
    service = group_service()
    tracker = GroupTracker(None, service, BOT_ID)
    event = SimpleNamespace(is_group=True, chat_id=CHAT_ID, id=1, date=NOW, chat=channel(min=True))
    await asyncio.gather(*(tracker.observe(event) for _ in range(1000)))
    service.record.assert_awaited_once()
    assert service.record.call_args.args[0].admin is None


async def test_inactive_group_recovery_needs_definite_current_self_membership():
    service = group_service()
    service.get.return_value = SimpleNamespace(
        is_active=False,
        is_admin=False,
        migrated_to=None,
        last_event_at=datetime(2026, 10, 14, tzinfo=UTC),
    )
    client = SimpleNamespace(
        get_permissions=AsyncMock(
            return_value=SimpleNamespace(participant=types.ChatParticipant(BOT_ID, 50, NOW))
        )
    )
    tracker = GroupTracker(client, service, BOT_ID)
    chat = types.Chat(77, "Group", types.ChatPhotoEmpty(), 5, NOW, 1)
    await tracker.observe(SimpleNamespace(is_group=True, chat_id=-77, id=42, date=NOW, chat=chat))
    assert service.record.call_args_list[0].args[0].kind == "observed"
    assert service.record.call_args_list[1].args[0].kind == "standing_refreshed"
    assert service.record.call_args_list[1].args[0].active is True


async def test_old_activity_with_definitely_left_entity_does_not_discover_active_group():
    service = group_service()
    await GroupTracker(None, service, BOT_ID).observe(
        SimpleNamespace(is_group=True, chat_id=CHAT_ID, id=42, date=NOW, chat=channel(left=True))
    )
    assert service.record.call_args.args[0].active is False


@pytest.mark.parametrize(
    ("site", "prefix", "url"),
    [
        ("youtube", "yt", "https://youtu.be/jNQXAC9IVRw"),
        ("instagram", "ig", "https://www.instagram.com/reel/DeR9eU8idi0/"),
        ("soundcloud", "sc", "https://soundcloud.com/quavoofficial/away"),
        ("pinterest", "pin", "https://www.pinterest.com/pin/123456789/"),
        ("xvideos", "xv", "https://www.xvideos.com/video.demo/test"),
        ("xnxx", "xnxx", "https://www.xnxx.com/video-demo/test"),
        ("tiktok", "tt", "https://www.tiktok.com/@test/video/6718335390845095173"),
    ],
)
async def test_every_provider_replies_with_menu_and_delivers_in_same_group(site, prefix, url):
    registrations, responses = [], []
    groups = group_service()
    client = SimpleNamespace(
        add_event_handler=lambda cb, builder: registrations.append((cb, builder))
    )
    menus = MenuStore()
    quality = Quality("original", "Original", "h264", None, "mp4", "video/mp4", "progressive", "")
    media = Media(site, "id", "Title", "Author", 5, url, None, (quality,))

    async def respond(text, **options):
        position = len(responses)
        responses.append((text, options))

        async def edit(text, **edited):
            responses[position] = (text, {**options, **edited})

        return SimpleNamespace(edit=AsyncMock(side_effect=edit))

    async def deliver(peer, actual, selected, progress):
        assert peer is PEER and actual is media and selected is quality
        assert request_chat.get() == CHAT_ID and request_message.get() == 42
        progress.phase = "done"

    service = SimpleNamespace(
        inspect=AsyncMock(return_value=media), deliver=AsyncMock(side_effect=deliver)
    )
    limits = RequestLimiter(60, groups=groups)
    access = ProviderAccess(7)
    register_handlers(
        client, service, menus, limits=limits, groups=groups, bot_id=BOT_ID, access=access
    )
    event = SimpleNamespace(
        raw_text=url,
        sender_id=7,
        chat_id=CHAT_ID,
        id=42,
        respond=respond,
        is_group=True,
        is_channel=True,
    )
    callback = next(
        cb
        for cb, builder in registrations
        if getattr(builder, "pattern", None) and builder.pattern(url)
    )
    await callback(event)
    assert responses[0][1]["reply_to"] == 42
    button = responses[0][1]["buttons"][0][0]
    name, token, index = button.type.data.decode().split(":")
    assert name == prefix
    menu = await menus.fetch(token, 7, CHAT_ID)
    assert menu.message_id == 42
    tapped = SimpleNamespace(
        sender_id=7,
        chat_id=CHAT_ID,
        id=100,
        respond=respond,
        answer=AsyncMock(),
        get_input_chat=AsyncMock(return_value=PEER),
        pattern_match=(None, token.encode(), index.encode()),
    )
    await handle_quality(tapped, service, menus, limits=limits, access=access)
    service.deliver.assert_awaited_once()
    assert responses[1][1]["reply_to"] == 42
    assert groups.claim_link.await_count == 1  # A quality click is not another submitted link.
    for user, chat in ((8, CHAT_ID), (7, -88)):
        tapped.sender_id, tapped.chat_id = user, chat
        await handle_quality(tapped, service, menus)
    assert service.deliver.await_count == 1


async def test_group_file_publication_replies_to_original_link():
    client = SimpleNamespace(send_file=AsyncMock(return_value="sent"))
    delivery = TelegramDelivery(client, None, "ffmpeg")
    with user_request(7, CHAT_ID, message_id=42):
        assert await delivery._send_file(PEER, b"test", caption="caption") == "sent"
    assert client.send_file.call_args.kwargs["reply_to"] == 42
    assert utils.get_peer_id(client.send_file.call_args.args[0]) == CHAT_ID


async def test_start_in_group_is_silent_and_has_no_onboarding_side_effects():
    users = SimpleNamespace(start=AsyncMock(), touch_user=AsyncMock())
    event = SimpleNamespace(sender_id=7, chat_id=CHAT_ID, id=42, is_group=True, respond=AsyncMock())
    await handle_start(event, users=users)
    users.start.assert_not_awaited()
    users.touch_user.assert_not_awaited()
    event.respond.assert_not_awaited()


async def test_group_rate_warnings_coalesce_across_different_users_and_reply_to_link():
    replies = []

    async def respond(text, **options):
        replies.append(options)

    wrapper = LimitedHandlers(None, RequestLimiter())
    await asyncio.gather(
        *(
            wrapper._respond(
                SimpleNamespace(sender_id=user, chat_id=CHAT_ID, id=user, respond=respond),
                "Group quota reached",
                reason="group_rate_limited",
            )
            for user in range(1, 1001)
        )
    )
    assert len(replies) == 1 and replies[0]["reply_to"] == 1


@pytest.mark.parametrize("known", [None, SimpleNamespace(chat_kind="supergroup")])
async def test_cold_callback_entity_cache_uses_persisted_group_kind(known):
    callbacks = []
    groups = group_service()
    groups.get.return_value = known
    handler = AsyncMock()
    dispatcher = Dispatcher(
        SimpleNamespace(add_event_handler=lambda cb, _: callbacks.append(cb)), groups
    )
    dispatcher.add_event_handler(handler, None)
    await callbacks[0](
        SimpleNamespace(sender_id=7, chat_id=CHAT_ID, is_channel=True, is_group=None)
    )
    assert handler.await_count == (1 if known else 0)


@pytest.mark.parametrize("value", [True, 0, -50, types.PeerChannel(50), None])
def test_actor_identity_never_invents_a_human(value):
    assert human_id(value) is None


def test_domain_rejects_positive_group_ids_and_naive_times():
    with pytest.raises(ValueError):
        GroupChange(7, "added", "key", NOW, "group")
    with pytest.raises(ValueError):
        GroupChange(-7, "added", "key", NOW.replace(tzinfo=None), "group")
    assert group_kind(types.User(1)) is None
    assert participant_standing(object()) == (None, None, None)


@pytest.mark.parametrize("value", ["0", "1001", "invalid"])
def test_group_env_setting_validates_range(monkeypatch, value):
    for key in ("API_ID", "API_HASH", "BOT_TOKEN", "DATABASE_URL"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("GROUP_LINKS_PER_MINUTE", value)
    with pytest.raises(ConfigurationError) as error:
        Settings.from_environment()
    assert error.value.fields == ("GROUP_LINKS_PER_MINUTE",)
