"""Couplyo field coverage and lifecycle behavior on real SQL and the installed Telegram SDK."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import MemorySession
from test_admin_reference_database import app as app

from bot.handlers.groups import GroupTracker
from bot.handlers.membership import MembershipTracker
from db.postgres.engine import migrate
from schemas.group import GroupChange, GroupStanding, Standing
from schemas.membership import MembershipEvent

NOW = datetime.now(UTC)


def human(identity, role=None, **values):
    user = types.User(identity, first_name=f"Human {identity}", **values)
    user.participant = role
    return user


class BasicChatClient(TelegramClient):
    """Use Telethon's actual participant iterator without connecting to Telegram."""

    def __init__(self, full):
        super().__init__(MemorySession(), 1, "test", receive_updates=False)
        self.full, self.requests = full, []

    async def __call__(self, request, *args, **kwargs):
        self.requests.append(request)
        assert isinstance(request, functions.messages.GetFullChatRequest)
        return self.full


async def test_basic_group_snapshot_does_not_mistake_sdk_unfiltered_members_for_admins(app):
    roles = [
        types.ChatParticipantCreator(7),
        types.ChatParticipantAdmin(8, 7, NOW),
        types.ChatParticipant(9, 7, NOW),
        types.ChatParticipantAdmin(10, 7, NOW),
        types.ChatParticipantAdmin(11, 7, NOW),
    ]
    users = [human(7), human(8), human(9), human(10, bot=True), human(11, deleted=True)]
    entity = types.Chat(10, "Basic", types.ChatPhotoEmpty(), 5, NOW, 1)
    full = types.messages.ChatFull(
        types.ChatFull(10, "", types.ChatParticipants(10, roles, 1), types.PeerNotifySettings()),
        [entity],
        users,
    )
    client = BasicChatClient(full)
    try:
        sdk_users = await client.get_participants(
            types.InputPeerChat(10), filter=types.ChannelParticipantsAdmins()
        )
        assert {item.id for item in sdk_users} == {7, 8, 9, 10, 11}
        await GroupTracker(client, app.groups, 123).snapshot(entity, -10)
        stored = await app.groups.get(-10, fresh=True)
        assert stored.owner_id == 7 and stored.admin_ids == (8,)
        assert stored.member_count == 5
    finally:
        await client.disconnect()


@pytest.mark.parametrize("admin_failure", [False, True])
async def test_supergroup_snapshot_keeps_real_count_existing_invite_and_partial_contacts(
    app, admin_failure
):
    chat = -(10**12 + 77)
    await app.groups.record(
        GroupChange(chat, "added", "seed", app.now, "supergroup", active=True, admin=True)
    )
    await app.groups.metadata(chat, 7, [8], 5, None)
    entity = types.Channel(
        77, "Private", types.ChatPhotoEmpty(), NOW, megagroup=True, access_hash=4321
    )
    users = [
        human(7, types.ChannelParticipantCreator(7, types.ChatAdminRights())),
        human(8, types.ChannelParticipantAdmin(8, 7, NOW, types.ChatAdminRights())),
        human(9, types.ChannelParticipant(9, NOW)),
    ]
    client = AsyncMock(
        return_value=SimpleNamespace(
            full_chat=SimpleNamespace(
                participants_count=321,
                exported_invite=SimpleNamespace(link="https://t.me/+test-invite"),
            )
        )
    )
    client.get_participants = (
        AsyncMock(side_effect=errors.ChatAdminRequiredError(None))
        if admin_failure
        else AsyncMock(return_value=users)
    )
    await GroupTracker(client, app.groups, 123).snapshot(entity, chat)
    stored = await app.groups.get(chat, fresh=True)
    assert stored.owner_id == 7 and stored.admin_ids == (8,)
    assert stored.member_count == 321 and stored.link == "https://t.me/+test-invite"
    request = client.call_args.args[0]
    assert isinstance(request, functions.channels.GetFullChannelRequest)
    assert request.channel is entity and request.channel.access_hash == 4321


async def test_forbidden_basic_roster_does_not_clear_known_administrators(app):
    await app.groups.metadata(-10, 7, [8], 50, None)
    entity = types.Chat(10, "Basic", types.ChatPhotoEmpty(), 50, NOW, 1)
    client = AsyncMock(
        return_value=SimpleNamespace(
            full_chat=SimpleNamespace(participants=types.ChatParticipantsForbidden(10)), users=[]
        )
    )
    await GroupTracker(client, app.groups, 123).snapshot(entity, -10)
    stored = await app.groups.get(-10, fresh=True)
    assert stored.owner_id == 7 and stored.admin_ids == (8,)
    client.get_participants.assert_not_awaited()


async def test_metadata_normalizes_contacts_and_preserves_unresolved_fields(app):
    await app.groups.metadata(-10, 7, [7, 9, 8, 8], 10, "https://t.me/test_group")
    stored = await app.groups.get(-10, fresh=True)
    assert stored.admin_ids == (8, 9)
    await app.groups.metadata(-10, None, None, 12, None)
    stored = await app.groups.get(-10, fresh=True)
    assert (stored.owner_id, stored.admin_ids, stored.member_count, stored.link) == (
        7,
        (8, 9),
        12,
        "https://t.me/test_group",
    )
    await app.groups.metadata(-10, None, [], None, None)
    assert (await app.groups.get(-10, fresh=True)).admin_ids == ()


async def test_health_refresh_repairs_existing_contacts_without_restarting_a_crawl(app):
    await app.groups.record(GroupChange(-10, "promoted", "admin", app.now, "group", admin=True))
    await app.groups.metadata(-10, 7, [8, 9], 999, None)
    roles = [
        types.ChatParticipantCreator(7),
        types.ChatParticipantAdmin(8, 7, NOW),
        types.ChatParticipant(9, 7, NOW),
    ]
    entity = types.Chat(10, "Basic", types.ChatPhotoEmpty(), 3, NOW, 1)
    full = types.messages.ChatFull(
        types.ChatFull(10, "", types.ChatParticipants(10, roles, 1), types.PeerNotifySettings()),
        [entity],
        [human(7), human(8), human(9)],
    )
    client = BasicChatClient(full)
    client.get_entity = AsyncMock(return_value=entity)
    tracker = GroupTracker(
        client,
        app.groups,
        123,
        probe=SimpleNamespace(check=AsyncMock(return_value=GroupStanding(Standing.ADMIN, True))),
    )
    try:
        assert (await tracker.health(-10)).standing is Standing.ADMIN
        stored = await app.groups.get(-10, fresh=True)
        assert stored.admin_ids == (8,) and stored.member_count == 3
        assert await app.pool.fetchval("SELECT count(*) FROM group_roster_crawls") == 0
    finally:
        await client.disconnect()


@pytest.mark.parametrize("standing", [True, False, None])
async def test_first_group_message_uses_the_persisted_admin_standing_after_discovery(standing):
    group = SimpleNamespace(is_admin=standing)
    groups = SimpleNamespace(get=AsyncMock(side_effect=[None, None, group]))
    parent = SimpleNamespace(observe=AsyncMock())
    memberships = SimpleNamespace(observe=AsyncMock())
    entity = types.Chat(10, "Basic", types.ChatPhotoEmpty(), 1, NOW, 1)
    update = SimpleNamespace(
        is_group=True, sender_id=7, sender=human(7), chat_id=-10, chat=entity, id=1, date=NOW
    )
    await MembershipTracker(None, groups, parent, memberships, 123).observe(update)
    assert memberships.observe.call_args.args[0].observed_admin is standing


async def test_join_leave_rejoin_timestamps_do_not_fabricate_messages_or_start_consent(app):
    joined = MembershipEvent(
        -10, 7, "join", "joined", app.now, 1, first_name="Human", stream="participant"
    )
    assert await app.membership.persist(joined)
    row = await app.pool.fetchrow("SELECT * FROM group_memberships WHERE user_id=7")
    assert row["last_seen_at"] == app.now and row["message_count"] == row["msg_this_week"] == 0
    await app.membership.persist(
        MembershipEvent(
            -10, 7, "left", "left", app.now + timedelta(seconds=5), 2, stream="participant"
        )
    )
    await app.membership.persist(
        MembershipEvent(-10, 7, "old-message", "message", app.now + timedelta(seconds=1))
    )
    assert not await app.pool.fetchval("SELECT is_active FROM group_memberships WHERE user_id=7")
    await app.membership.persist(
        MembershipEvent(
            -10, 7, "rejoin", "joined", app.now + timedelta(seconds=10), 3, stream="participant"
        )
    )
    row = await app.pool.fetchrow("SELECT * FROM group_memberships WHERE user_id=7")
    assert row["is_active"] and row["last_seen_at"] == app.now + timedelta(seconds=10)
    assert row["message_count"] == row["msg_this_week"] == 1
    assert not await app.pool.fetchval("SELECT is_started FROM bot_users WHERE user_id=7")


async def test_all_source_group_and_membership_fields_exist_after_idempotent_migration(app):
    expected = {
        "bot_groups": {
            "chat_id",
            "title",
            "is_active",
            "is_admin",
            "added_at",
            "readded_at",
            "removed_at",
            "admin_since",
            "last_activity_at",
            "owner_id",
            "added_by_user_id",
            "admin_ids",
            "member_count",
            "can_send",
            "link",
            "created_at",
            "updated_at",
        },
        "group_memberships": {
            "id",
            "chat_id",
            "user_id",
            "is_active",
            "last_seen_at",
            "state_changed_at",
            "state_event_order",
            "msg_week",
            "msg_this_week",
            "msg_last_week",
            "created_at",
        },
        "group_roster_crawls": {
            "chat_id",
            "status",
            "pass_number",
            "filter_index",
            "offset_count",
            "pass_new_count",
            "claimed_total",
            "seen_unique",
            "stored_new",
            "skipped_accounts",
            "requests_made",
            "flood_waits",
            "converged",
            "stopped_because",
            "last_error",
            "available_at",
            "started_at",
            "updated_at",
            "finished_at",
            "created_at",
        },
    }
    await app.membership.persist(MembershipEvent(-10, 7, "message", "message", app.now))
    before = await app.pool.fetchrow("SELECT * FROM group_memberships")
    await migrate(app.pool)
    await migrate(app.pool)
    assert await app.pool.fetchrow("SELECT * FROM group_memberships") == before
    for table, columns in expected.items():
        actual = await app.pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=$1",
            table,
        )
        assert columns <= {row["column_name"] for row in actual}
    assert await app.pool.fetchval("SELECT max(version) FROM app_schema_migrations") == 13


async def test_thousand_replayed_joins_and_messages_create_one_member_and_exact_counters(app):
    joined = MembershipEvent(-10, 7, "join", "joined", app.now, stream="participant")
    await asyncio.gather(*(app.membership.persist(joined) for _ in range(1000)))
    message = MembershipEvent(
        -10, 7, "message", "message", app.now + timedelta(seconds=1), observed_admin=True
    )
    await asyncio.gather(*(app.membership.persist(message) for _ in range(1000)))
    assert await app.pool.fetchval("SELECT count(*) FROM group_memberships") == 1
    assert await app.pool.fetchval("SELECT message_count FROM group_memberships") == 1
    assert await app.pool.fetchval("SELECT count(*) FROM membership_events") == 2


@pytest.mark.parametrize("basic", [False, True])
async def test_raw_bot_lifecycle_persists_contacts_timestamps_audit_and_roster_transitions(
    app, basic
):
    chat = -77 if basic else -(10**12 + 77)
    when = app.now - timedelta(seconds=20)
    entity = (
        types.Chat(77, "Lifecycle", types.ChatPhotoEmpty(), 12, when, 1)
        if basic
        else types.Channel(
            77, "Lifecycle", types.ChatPhotoEmpty(), when, megagroup=True, access_hash=987
        )
    )
    users = [
        human(7, types.ChatParticipantCreator(7)),
        human(8, types.ChatParticipantAdmin(8, 7, when)),
        human(9, types.ChatParticipant(9, 7, when)),
    ]
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=entity), get_participants=AsyncMock(return_value=users)
    )
    status = Standing.MEMBER

    async def check(*args):
        return GroupStanding(status, status is not Standing.GONE)

    tracker = GroupTracker(
        client, app.groups, 123, probe=SimpleNamespace(check=check), settle_pause=0
    )
    app.groups.roster_enabled = True
    if basic:
        updates = [
            types.UpdateChatParticipantAdd(77, 123, 50, when, 1),
            types.UpdateChatParticipantAdmin(77, 123, True, 2),
            types.UpdateChatParticipantAdmin(77, 123, False, 3),
            types.UpdateChatParticipantDelete(77, 123, 4),
            types.UpdateChatParticipantAdd(77, 123, 51, app.now, 5),
            types.UpdateChatParticipantAdmin(77, 123, True, 6),
        ]
    else:
        member = types.ChannelParticipant(123, when)
        admin = types.ChannelParticipantAdmin(123, 50, when, types.ChatAdminRights())
        transitions = [
            (None, member),
            (member, admin),
            (admin, member),
            (member, None),
            (None, member),
            (member, admin),
        ]
        updates = [
            types.UpdateChannelParticipant(
                77,
                when + timedelta(seconds=index),
                50 if index < 4 else 51,
                123,
                index + 1,
                prev_participant=old,
                new_participant=new,
            )
            for index, (old, new) in enumerate(transitions)
        ]
    original_add = None
    previous_run = None
    removed_at = None
    for index, update in enumerate(updates):
        status = Standing.GONE if index == 3 else Standing.MEMBER
        await tracker.handle_update(update)
        group = await app.groups.get(chat, fresh=True)
        if index == 0:
            original_add = group.added_at
        assert group.added_at == original_add and group.added_by_user_id == 50
        if index in {1, 5}:
            assert group.is_active and group.is_admin and group.admin_since
            assert group.owner_id == 7 and group.admin_ids == (8,)
            crawl = await app.pool.fetchrow(
                "SELECT run_id,status FROM group_roster_crawls WHERE chat_id=$1", chat
            )
            assert crawl["status"] == "queued" and crawl["run_id"] != previous_run
            previous_run = crawl["run_id"]
        if index == 1:
            await app.membership.persist(
                MembershipEvent(chat, 7, "human-message", "message", app.now)
            )
        if index in {2, 3}:
            assert group.is_active is (index == 2) and group.admin_since is None
            assert (
                await app.pool.fetchval(
                    "SELECT message_count FROM group_memberships WHERE chat_id=$1 AND user_id=7",
                    chat,
                )
                == 1
            )
            assert await app.pool.fetchval(
                "SELECT status FROM group_roster_crawls WHERE chat_id=$1", chat
            ) == ("paused" if index == 2 else "cancelled")
        if index == 3:
            removed_at = group.removed_at
        if index >= 4:
            assert group.removed_at == removed_at and group.readded_at
    assert not await app.pool.fetchval("SELECT is_admin FROM bot_users WHERE user_id=7")
    before = await app.pool.fetchval("SELECT count(*) FROM group_events WHERE chat_id=$1", chat)
    await tracker.handle_update(updates[-1])
    assert (
        await app.pool.fetchval("SELECT count(*) FROM group_events WHERE chat_id=$1", chat)
        == before
    )
