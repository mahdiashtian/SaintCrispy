"""Registration survives cold entities and infrastructure failure without invented identities."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from bot.handlers.groups import GroupTracker
from bot.handlers.membership import MembershipTracker
from schemas.group import GroupChange

NOW = datetime.now(UTC)


@pytest.mark.parametrize("cached", [False, True])
async def test_cold_private_message_registers_verified_sender(cached):
    user = types.User(id=7, first_name="Human")
    client = SimpleNamespace(get_entity=AsyncMock(return_value=user))
    memberships = SimpleNamespace(profile=AsyncMock())
    tracker = MembershipTracker(client, None, None, memberships, 123)
    await tracker.observe_private(
        SimpleNamespace(sender=None, sender_id=7, chat_id=7, _entities={7: user} if cached else {})
    )
    memberships.profile.assert_awaited_once()
    assert memberships.profile.call_args.args[0] == 7


async def test_member_resolution_does_not_confuse_inviter_with_member():
    user = types.User(id=7, first_name="Member")
    tracker = MembershipTracker(None, None, None, None, 123)
    result = await tracker.human(7, SimpleNamespace(sender=types.User(id=9), _entities={7: user}))
    assert result is user


async def test_group_observation_is_durable_before_optional_sql_lookup():
    entity = types.ChatForbidden(id=77, title="Group")
    service = SimpleNamespace(
        get=AsyncMock(side_effect=OSError("SQL unavailable")),
        record=AsyncMock(return_value=None),
    )
    tracker = GroupTracker(None, service, 123)
    await tracker.observe(SimpleNamespace(is_group=True, chat_id=-77, chat=entity, id=1, date=NOW))
    service.record.assert_awaited_once()
    assert service.record.call_args.args[0].chat_id == -77


async def test_member_is_submitted_when_parent_creation_is_durably_pending():
    entity = types.ChatForbidden(id=77, title="Group")
    groups = SimpleNamespace(get=AsyncMock(side_effect=OSError("SQL unavailable")))
    group_tracker = SimpleNamespace(_entity=AsyncMock(return_value=entity), observe=AsyncMock())
    memberships = SimpleNamespace(observe=AsyncMock())
    tracker = MembershipTracker(None, groups, group_tracker, memberships, 123)
    await tracker.observe(
        SimpleNamespace(
            is_group=True,
            chat_id=-77,
            sender_id=7,
            sender=types.User(id=7),
            chat=entity,
            id=1,
            date=NOW,
        )
    )
    group_tracker.observe.assert_awaited_once()
    memberships.observe.assert_awaited_once()


@pytest.mark.parametrize("field", ["title", "username", "event_key", "kind", "stream"])
def test_lifecycle_rejects_postgres_nul_poison(field):
    values = dict(chat_id=-77, kind="observed", event_key="one", occurred_at=NOW, chat_kind="group")
    values[field] = "bad\x00value"
    with pytest.raises(ValueError):
        GroupChange(**values)


async def test_cold_sender_uses_message_peer_resolution_before_bare_id_lookup():
    user = types.User(id=7)
    client = SimpleNamespace(get_entity=AsyncMock(side_effect=ValueError("cold input peer")))
    tracker = MembershipTracker(client, None, None, None, 123)
    assert await tracker.human(7, SimpleNamespace(get_sender=AsyncMock(return_value=user))) is user
    client.get_entity.assert_not_awaited()


async def test_forbidden_group_metadata_does_not_assert_current_membership():
    service = SimpleNamespace(record=AsyncMock(return_value=None))
    await GroupTracker(None, service, 123).observe(
        SimpleNamespace(
            is_group=True, chat_id=-77, chat=types.ChatForbidden(77, "Group"), id=1, date=NOW
        )
    )
    assert service.record.call_args.args[0].active is None


async def test_failed_optional_probe_does_not_suppress_next_recovery_check():
    from schemas.group import GroupStanding, Standing

    entity = types.Chat(77, "Group", types.ChatPhotoEmpty(), 1, NOW, 1)
    service = SimpleNamespace(
        record=AsyncMock(return_value={"applied": True}),
        get=AsyncMock(
            return_value=SimpleNamespace(migrated_to=None, is_admin=False, last_event_at=NOW)
        ),
    )
    probe = SimpleNamespace(check=AsyncMock(return_value=GroupStanding(Standing.UNKNOWN)))
    tracker = GroupTracker(None, service, 123, probe=probe)
    await tracker.observe(SimpleNamespace(is_group=True, chat_id=-77, chat=entity, id=1, date=NOW))
    assert -77 not in tracker._checked


@pytest.mark.parametrize("user,chat", [(2**63, 7), (7, -(2**63)), (True, 7), (7, 2**63)])
def test_dispatch_rejects_non_telegram_integer_boundaries(user, chat):
    from bot.checks.identity import has_user_context

    assert not has_user_context(SimpleNamespace(sender_id=user, chat_id=chat))


async def test_basic_self_update_is_queued_without_optional_sql_or_entity():
    service = SimpleNamespace(
        record=AsyncMock(return_value=None),
        get=AsyncMock(side_effect=OSError("SQL unavailable")),
    )
    client = SimpleNamespace(get_entity=AsyncMock(side_effect=ValueError("cold peer")))
    await GroupTracker(client, service, 123).handle_update(
        types.UpdateChatParticipantAdd(77, 123, 9, NOW, 1)
    )
    service.record.assert_awaited_once()
    service.get.assert_not_awaited()
    assert service.record.call_args.args[0].chat_kind == "group"


async def test_basic_join_saves_parent_identity_without_asserting_bot_standing():
    groups = SimpleNamespace(get=AsyncMock(return_value=None), record=AsyncMock())
    tracker = MembershipTracker(
        None,
        groups,
        SimpleNamespace(_entity=AsyncMock(return_value=None)),
        None,
        123,
    )
    assert await tracker.ensure_group(-77, types.UpdateChatParticipantAdd(77, 7, 9, NOW, 1))
    saved = groups.record.call_args.args[0]
    assert saved.chat_kind == "group" and saved.active is None and saved.admin is None


@pytest.mark.parametrize(
    "sender", [types.User(id=7, bot=True), types.User(id=7, deleted=True), types.User(id=8)]
)
async def test_known_invalid_sender_never_reaches_user_creating_handler(sender):
    from bot.handlers.dispatcher import Dispatcher

    callbacks = []
    dispatcher = Dispatcher(
        SimpleNamespace(add_event_handler=lambda callback, _: callbacks.append(callback))
    )
    handler = AsyncMock()
    dispatcher.add_event_handler(handler, None)
    await callbacks[0](SimpleNamespace(sender_id=7, chat_id=7, sender=sender))
    handler.assert_not_awaited()
