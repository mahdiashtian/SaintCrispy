"""Telegram-shaped panel, lifecycle, sender and delivery boundaries without live RPCs."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, events, types

from bot.handlers import admin as ui
from bot.handlers.dispatcher import Dispatcher
from bot.handlers.groups import GroupTracker
from bot.handlers.membership import MembershipTracker
from bot.keyboards.admin import button, compact_id, home
from bot.operations import EffectDelivery, OperationalWorkers
from bot.premium_emoji import BotPresentation
from schemas.admin import PendingEffect, PermanentEffect, RetryEffect
from schemas.group import GroupStanding, SavedGroupPeer, Standing
from schemas.media import DownloadError

NOW = datetime.now(UTC)
CHAT = -1000000000077


def event(owner=999, chat=None, data=None, **values):
    return SimpleNamespace(
        sender_id=owner,
        chat_id=owner if chat is None else chat,
        data=data,
        respond=AsyncMock(return_value=SimpleNamespace(id=42)),
        edit=AsyncMock(),
        answer=AsyncMock(),
        **values,
    )


def channel():
    return types.Channel(
        id=77,
        title="Group",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        megagroup=True,
        access_hash=12345,
    )


@pytest.mark.parametrize(
    "owner,chat,data", [(7, 7, b"adm:999:stats:0"), (999, CHAT, b"adm:999:stats:0")]
)
async def test_forged_or_group_callbacks_do_not_reach_authorization_or_data(owner, chat, data):
    admin = SimpleNamespace(authorize=AsyncMock(), stats=AsyncMock())
    actual = event(owner, chat, data)
    await ui.callback(actual, admin)
    admin.authorize.assert_not_awaited()
    admin.stats.assert_not_awaited()
    assert actual.answer.call_args.kwargs["alert"]


async def test_private_panel_denies_unconfigured_or_non_admin_user():
    admin = SimpleNamespace(
        authorize=AsyncMock(side_effect=DownloadError("Denied", code="admin_denied"))
    )
    actual = event()
    await ui.open_panel(actual, admin)
    assert "⛔" in actual.respond.call_args.args[0]


async def test_prompt_persists_owner_and_outgoing_reply_id_before_accepting_input():
    admin = SimpleNamespace(
        authorize=AsyncMock(return_value=True),
        prompt=AsyncMock(return_value="abc"),
        attach_prompt=AsyncMock(),
    )
    actual = event(data=b"adm:999:prompt:grant")
    await ui.callback(actual, admin)
    admin.prompt.assert_awaited_once_with(999, "grant", {})
    admin.attach_prompt.assert_awaited_once_with(999, "abc", 42)
    assert isinstance(actual.respond.call_args.kwargs["buttons"], types.ReplyKeyboardForceReply)


async def test_admin_content_reply_is_consumed_before_provider_link_routing():
    admin = SimpleNamespace(
        find_prompt=AsyncMock(return_value={"token": "form"}),
        consume_prompt=AsyncMock(return_value=("broadcast", {"audience": "groups"})),
        prepare=AsyncMock(return_value="confirm"),
    )
    actual = event(reply_to_msg_id=42, raw_text="https://www.pinterest.com/pin/123/", id=50)
    callbacks = []
    dispatcher = Dispatcher(
        SimpleNamespace(add_event_handler=lambda callback, builder: callbacks.append(callback))
    )
    from functools import partial

    dispatcher.add_event_handler(partial(ui.input_reply, admin=admin), None)
    with pytest.raises(events.StopPropagation):
        await callbacks[0](actual)
    admin.prepare.assert_awaited_once()
    assert admin.prepare.call_args.args[2]["text"] == actual.raw_text


async def test_unrelated_private_reply_is_not_consumed():
    admin = SimpleNamespace(find_prompt=AsyncMock(return_value=None))
    actual = event(reply_to_msg_id=42, raw_text="ordinary reply")
    await ui.input_reply(actual, admin)
    actual.respond.assert_not_awaited()


def test_owner_callbacks_fit_even_at_bigint_boundaries():
    owner = 2**63 - 1
    for row in home(owner, True):
        for item in row:
            assert len(item.type.data) <= 64
    payload = f"{compact_id(-(2**63) + 1)}_{compact_id(2**63 - 1)}"
    assert len(button(owner, "Members", "members", payload).type.data) <= 64
    assert tuple(int(value, 36) for value in payload.split("_")) == (-(2**63) + 1, 2**63 - 1)


@pytest.mark.parametrize(
    "human",
    [
        None,
        types.User(id=7, bot=True),
        types.User(id=7, deleted=True),
        types.UserEmpty(7),
        types.Channel(
            id=7, title="Channel", photo=types.ChatPhotoEmpty(), date=NOW, broadcast=True
        ),
    ],
)
async def test_unknown_bot_deleted_and_channel_senders_do_not_create_human_memberships(human):
    memberships = SimpleNamespace(observe=AsyncMock())
    tracker = MembershipTracker(
        SimpleNamespace(get_entity=AsyncMock(return_value=human)),
        SimpleNamespace(get=AsyncMock(return_value=object())),
        None,
        memberships,
        123,
    )
    await tracker.observe(
        SimpleNamespace(is_group=True, chat_id=CHAT, sender_id=7, sender=human, id=1, date=NOW)
    )
    memberships.observe.assert_not_awaited()


async def test_all_human_group_messages_including_media_are_recorded():
    memberships = SimpleNamespace(observe=AsyncMock())
    tracker = MembershipTracker(
        None, SimpleNamespace(get=AsyncMock(return_value=object())), None, memberships, 123
    )
    actual = SimpleNamespace(
        is_group=True,
        chat_id=CHAT,
        sender_id=7,
        sender=types.User(id=7, first_name="Human"),
        id=5,
        date=NOW,
        raw_text="",
        media=object(),
    )
    await tracker.observe(actual)
    recorded = memberships.observe.call_args.args[0]
    assert (
        recorded.kind == "message" and recorded.user_id == 7 and recorded.event_key == "message:5"
    )


async def test_settled_add_and_stale_demotion_preserve_confirmed_admin():
    service = SimpleNamespace(record=AsyncMock(return_value={"applied": True}), get=AsyncMock())
    probe = SimpleNamespace(
        check=AsyncMock(
            side_effect=[
                GroupStanding(Standing.MEMBER),
                GroupStanding(Standing.ADMIN, True),
                GroupStanding(Standing.ADMIN, True),
            ]
        )
    )
    tracker = GroupTracker(
        SimpleNamespace(get_entity=AsyncMock(return_value=channel())),
        service,
        123,
        probe=probe,
        settle_pause=0,
    )
    await tracker.handle_update(
        types.UpdateChannelParticipant(
            77, NOW, 5, 123, 1, new_participant=types.ChannelParticipant(123, NOW)
        )
    )
    assert service.record.call_args.args[0].admin is True
    await tracker.handle_update(
        types.UpdateChannelParticipant(
            77,
            NOW,
            5,
            123,
            2,
            prev_participant=types.ChannelParticipantAdmin(123, 5, NOW, types.ChatAdminRights()),
            new_participant=types.ChannelParticipant(123, NOW),
        )
    )
    assert service.record.call_args.args[0].admin is True
    assert service.record.call_args.args[0].kind == "standing_refreshed"


@pytest.mark.parametrize(
    "failure,removed,restricted",
    [
        (errors.UserNotParticipantError, True, False),
        (errors.ChatWriteForbiddenError, False, True),
        (errors.MediaEmptyError, False, False),
        (errors.ChannelPrivateError, False, False),
    ],
)
async def test_send_failures_separate_membership_send_permission_and_media(
    failure, removed, restricted
):
    service = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(chat_kind="supergroup")), record=AsyncMock()
    )
    tracker = GroupTracker(
        SimpleNamespace(get_entity=AsyncMock(return_value=channel())),
        service,
        123,
        probe=SimpleNamespace(
            check=AsyncMock(return_value=GroupStanding(Standing.UNKNOWN, reason="network"))
        ),
    )
    await tracker.send_failure(CHAT, failure(request=None))
    if removed or restricted:
        change = service.record.call_args.args[0]
        assert change.active is (False if removed else None)
        assert change.can_send is False
    else:
        service.record.assert_not_awaited()


async def test_saved_peer_lookup_restores_addressing_without_changing_membership():
    client = SimpleNamespace(
        get_entity=AsyncMock(side_effect=[ValueError("cold session"), channel()])
    )
    service = SimpleNamespace(
        peer=AsyncMock(return_value=SavedGroupPeer(CHAT, 77, "supergroup", 12345)),
        record=AsyncMock(),
    )
    assert (await GroupTracker(client, service, 123)._entity(CHAT)).id == 77
    peer = client.get_entity.call_args.args[0]
    assert isinstance(peer, types.InputPeerChannel) and peer.access_hash == 12345
    service.record.assert_not_awaited()
    assert "12345" not in repr(await service.peer(CHAT))


async def test_outbox_custom_emoji_fallback_reuses_the_exact_random_id():
    requests = []

    class Client:
        async def get_input_entity(self, value):
            return types.InputPeerUser(value, 0)

        async def __call__(self, request):
            requests.append(request)
            if request.entities:
                raise errors.PremiumAccountRequiredError(request=None)

    presentation = BotPresentation()
    presentation.emojis = {"✅": 123}
    adapter = EffectDelivery(Client(), None, None, presentation=presentation)
    effect = PendingEffect(1, "message", 999, {"text": "✅ Delivered"}, 456, None, 1)
    await adapter.deliver(effect)
    assert len(requests) == 2 and all(request.random_id == 456 for request in requests)
    assert presentation.disabled and not requests[-1].entities


async def test_workers_own_and_cancel_every_background_task():
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()

    workers = OperationalWorkers(
        SimpleNamespace(tick=blocked),
        SimpleNamespace(flush=blocked),
        groups=SimpleNamespace(flush_deferred=blocked),
    )
    async with workers:
        await started.wait()
    assert len(workers.tasks) == 3 and all(task.done() for task in workers.tasks)


async def test_forward_request_serializes_source_target_and_durable_random_id():
    requests = []

    class Client:
        async def get_input_entity(self, value):
            return types.InputPeerUser(value, 123)

        async def __call__(self, request):
            requests.append(request)
            bytes(request)

    effect = PendingEffect(1, "forward", 999, {"source_peer": 7, "source_id": 42}, 456, 1, 1)
    await EffectDelivery(Client(), None, None).deliver(effect)
    request = requests[0]
    assert request.from_peer.user_id == 7 and request.to_peer.user_id == 999
    assert request.id == [42] and request.random_id == [456]


@pytest.mark.parametrize("reason,deferred", [("network", False), ("persist_deferred", True)])
async def test_unknown_health_never_marks_group_removed_and_deferred_sql_is_retried(
    reason, deferred
):
    tracker = SimpleNamespace(
        health=AsyncMock(return_value=GroupStanding(Standing.UNKNOWN, reason=reason))
    )
    groups = SimpleNamespace(record=AsyncMock())
    with pytest.raises(RetryEffect if deferred else PermanentEffect):
        await EffectDelivery(None, tracker, groups).deliver(
            PendingEffect(1, "health", CHAT, {}, 456, 1, 1)
        )
    groups.record.assert_not_awaited()


async def test_replayed_leave_after_telegram_success_still_commits_local_removal():
    client = SimpleNamespace(
        get_input_entity=AsyncMock(side_effect=errors.UserNotParticipantError(request=None))
    )
    groups = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(is_active=True, chat_kind="supergroup")),
        record=AsyncMock(return_value=object()),
    )
    effect = PendingEffect(1, "leave", CHAT, {}, 456, 1, 2)
    await EffectDelivery(client, None, groups).deliver(effect)
    assert groups.record.call_args.args[0].active is False
    groups.record.return_value = None
    with pytest.raises(RetryEffect):
        await EffectDelivery(client, None, groups).deliver(effect)


async def test_outbox_recovers_saved_group_peer_after_cold_session_lookup():
    requests = []

    class Client:
        async def get_input_entity(self, value):
            raise ValueError("cold session")

        async def __call__(self, request):
            requests.append(request)
            bytes(request)

    groups = SimpleNamespace(
        peer=AsyncMock(return_value=SavedGroupPeer(CHAT, 77, "supergroup", 12345))
    )
    await EffectDelivery(Client(), None, groups).deliver(
        PendingEffect(1, "message", CHAT, {"text": "Notice"}, 456, None, 1)
    )
    assert requests[0].peer.channel_id == 77 and requests[0].random_id == 456


@pytest.mark.parametrize(
    "action,expected",
    [
        (types.MessageActionChatAddUser([7, 8, 123]), [(7, "joined"), (8, "joined")]),
        (types.MessageActionChatDeleteUser(7), [(7, "left")]),
        (types.MessageActionChatJoinedByLink(9), [(7, "joined")]),
        (types.MessageActionChatJoinedByRequest(), [(7, "joined")]),
    ],
)
async def test_human_service_join_leave_complements_raw_participants(action, expected):
    memberships = SimpleNamespace(
        observe=AsyncMock(), departure=AsyncMock(), known_member=AsyncMock(return_value=True)
    )
    groups = SimpleNamespace(get=AsyncMock(return_value=object()))
    tracker = MembershipTracker(None, groups, None, memberships, 123)
    message = types.MessageService(
        id=4, peer_id=types.PeerChat(77), date=NOW, from_id=types.PeerUser(7), action=action
    )
    update = types.UpdateNewMessage(message, 1, 1)
    update._entities = {
        7: types.User(id=7, first_name="One"),
        8: types.User(id=8, first_name="Two"),
    }
    await tracker.handle_update(update)
    actual = [call.args[0] for call in memberships.observe.call_args_list]
    assert [(item.user_id, item.kind) for item in actual] == expected
    assert len({item.event_key for item in actual}) == len(actual)
    assert all(item.stream == "service" for item in actual)
