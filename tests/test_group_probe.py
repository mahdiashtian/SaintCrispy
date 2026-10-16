"""Membership evidence stays independent of persistence and update reception."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

from bot.checks.probe import GroupProbe
from bot.handlers.groups import GroupTracker
from schemas.group import GroupChange, GroupStanding, Standing

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
CHAT_ID = -1000000000077
BOT_ID = 123


def channel(**values):
    return types.Channel(
        id=77, title="Group", photo=types.ChatPhotoEmpty(), date=NOW, megagroup=True, **values
    )


@pytest.mark.parametrize(
    "error",
    [
        errors.ChannelPrivateError(None),
        errors.ChannelInvalidError(None),
        errors.ChatAdminRequiredError(None),
        errors.FloodWaitError(None, 40),
        OSError(),
        TimeoutError(),
    ],
)
async def test_uncertain_errors_preserve_unknown_without_another_rpc(error):
    client = AsyncMock()
    client.get_permissions.side_effect = error
    verdict = await GroupProbe(client, BOT_ID).check(channel(access_hash=5), CHAT_ID)
    assert verdict.standing is Standing.UNKNOWN
    assert verdict.active is None and verdict.admin is None and verdict.can_send is None
    assert verdict.reason == type(error).__name__
    client.assert_not_awaited()


@pytest.mark.parametrize(
    "entity,chat_id",
    [(channel(), True), (channel(), 77), (channel(), -99), (types.User(77), CHAT_ID)],
)
async def test_invalid_or_mismatched_peers_never_trigger_network(entity, chat_id):
    client = SimpleNamespace(get_permissions=AsyncMock())
    result = await GroupProbe(client, BOT_ID).check(entity, chat_id)
    assert result.standing is Standing.UNKNOWN
    client.get_permissions.assert_not_awaited()


async def test_definite_not_participant_is_gone_but_empty_response_is_unknown():
    client = SimpleNamespace(
        get_permissions=AsyncMock(side_effect=errors.UserNotParticipantError(None))
    )
    probe = GroupProbe(client, BOT_ID)
    assert (await probe.check(channel(), CHAT_ID)).standing is Standing.GONE
    client.get_permissions.side_effect = None
    client.get_permissions.return_value = SimpleNamespace(participant=None)
    result = await probe.check(channel(), CHAT_ID)
    assert result.standing is Standing.UNKNOWN and result.reason == "incomplete_response"


async def test_missing_bot_input_user_recovers_with_valid_channel_hash_and_self():
    client = AsyncMock(
        return_value=SimpleNamespace(participant=types.ChannelParticipant(BOT_ID, NOW))
    )
    client.get_permissions.side_effect = ValueError("Input user absent")
    verdict = await GroupProbe(client, BOT_ID).check(channel(access_hash=5), CHAT_ID)
    assert verdict.standing is Standing.MEMBER
    request = client.call_args.args[0]
    assert isinstance(request, functions.channels.GetParticipantRequest)
    assert isinstance(request.participant, types.InputPeerSelf)
    assert request.channel.channel_id == 77 and request.channel.access_hash == 5


@pytest.mark.parametrize("values", [{}, {"access_hash": 5, "min": True}])
async def test_missing_or_minimal_channel_hash_never_uses_fabricated_hash(values):
    client = AsyncMock()
    client.get_permissions.side_effect = ValueError()
    verdict = await GroupProbe(client, BOT_ID).check(channel(**values), CHAT_ID)
    assert verdict.reason == "missing_access_hash"
    client.assert_not_awaited()


async def test_basic_group_fallback_requires_a_complete_correct_chat_roster():
    entity = types.Chat(77, "Group", types.ChatPhotoEmpty(), 5, NOW, 1)
    client = AsyncMock()
    client.get_permissions.side_effect = ValueError()
    probe = GroupProbe(client, BOT_ID)
    client.return_value = SimpleNamespace(full_chat=SimpleNamespace(participants=None))
    assert (await probe.check(entity, -77)).standing is Standing.UNKNOWN
    roster = types.ChatParticipants(77, [types.ChatParticipant(BOT_ID, 50, NOW)], 1)
    client.return_value.full_chat.participants = roster
    assert (await probe.check(entity, -77)).standing is Standing.MEMBER
    roster.chat_id = 99
    assert (await probe.check(entity, -77)).standing is Standing.UNKNOWN
    roster.chat_id, roster.participants = 77, []
    assert (await probe.check(entity, -77)).standing is Standing.GONE


async def test_restricted_member_is_present_and_inability_to_send_is_independent():
    member = types.ChannelParticipantBanned(
        types.PeerUser(BOT_ID), 50, NOW, types.ChatBannedRights(NOW, send_plain=True)
    )
    client = SimpleNamespace(
        get_permissions=AsyncMock(return_value=SimpleNamespace(participant=member))
    )
    verdict = await GroupProbe(client, BOT_ID).check(channel(), CHAT_ID)
    assert verdict.active is True and verdict.admin is False and verdict.can_send is False


async def test_probe_timeout_and_cancellation_release_slots():
    started = asyncio.Event()

    async def wait(*args):
        started.set()
        await asyncio.Future()

    probe = GroupProbe(SimpleNamespace(get_permissions=wait), BOT_ID, timeout=0.01, concurrency=1)
    assert (await probe.check(channel(), CHAT_ID)).standing is Standing.UNKNOWN
    probe.timeout = 60
    started.clear()
    task = asyncio.create_task(probe.check(channel(), CHAT_ID))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    probe.client.get_permissions = AsyncMock(side_effect=errors.UserNotParticipantError(None))
    assert (await probe.check(channel(), CHAT_ID)).standing is Standing.GONE


async def test_explicit_old_exit_cannot_overwrite_confirmed_current_membership():
    service = SimpleNamespace(record=AsyncMock(), get=AsyncMock(return_value=None))
    entity = channel()
    client = SimpleNamespace(get_entity=AsyncMock(return_value=entity))
    probe = SimpleNamespace(check=AsyncMock(return_value=GroupStanding(Standing.ADMIN, True)))
    tracker = GroupTracker(client, service, BOT_ID, probe=probe)
    await tracker.handle_update(types.UpdateChannelParticipant(77, NOW, 50, BOT_ID, 8))
    service.record.assert_awaited_once()
    change = service.record.call_args.args[0]
    assert change.kind == "standing_refreshed" and change.active and change.admin


@pytest.mark.parametrize("standing,calls", [(Standing.UNKNOWN, 2), (Standing.MEMBER, 1)])
async def test_lazy_check_debounces_confirmed_standing_and_retries_unknown(standing, calls):
    service = SimpleNamespace(
        record=AsyncMock(),
        get=AsyncMock(
            return_value=SimpleNamespace(
                is_admin=False, migrated_to=None, last_event_at=NOW - timedelta(days=1)
            )
        ),
    )
    probe = SimpleNamespace(check=AsyncMock(return_value=GroupStanding(standing)))
    tracker = GroupTracker(None, service, BOT_ID, probe=probe)
    event = SimpleNamespace(is_group=True, chat_id=CHAT_ID, chat=channel(), date=NOW, id=1)
    await tracker.observe(event)
    tracker._seen.clear()
    event.id = 2
    await tracker.observe(event)
    assert probe.check.await_count == calls
    if standing is Standing.UNKNOWN:
        assert all(call.args[0].kind == "observed" for call in service.record.call_args_list)
    else:
        refreshed = [
            call.args[0]
            for call in service.record.call_args_list
            if call.args[0].kind == "standing_refreshed"
        ]
        assert len(refreshed) == 1 and refreshed[0].active is True


@pytest.mark.parametrize("destination", [True, 0, 10, -77])
def test_migration_destinations_reject_booleans_private_and_same_chat(destination):
    with pytest.raises(ValueError):
        GroupChange(-77, "migrated", "migration", NOW, "group", migrated_to=destination)
