"""SQL/STATE crash recovery from actual Telegram adapters, not just service DTOs."""

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types
from test_admin_reference_database import app as app

from bot.handlers.groups import GroupTracker
from bot.handlers.membership import MembershipTracker
from repositories.redis.activity import LifecycleStream
from repositories.redis.objects import ObjectCache
from schemas.group import GroupChange
from schemas.membership import HumanProfile, MembershipEvent
from services.group import GroupService
from services.membership import MembershipService


@asynccontextmanager
async def unavailable():
    raise OSError("test SQL outage")
    yield


async def test_private_profile_outage_replays_after_restart_without_start_or_roles(app):
    service = MembershipService(None, unavailable, app.stream)
    assert await service.profile(7, "human", "Human", None) is False
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 0
    assert await app.membership.flush() == 1
    row = await app.pool.fetchrow("SELECT * FROM bot_users WHERE user_id=7")
    assert row["account_kind"] == "real" and row["first_name"] == "Human"
    assert not row["is_started"] and not row["is_admin"] and not row["is_banned"]
    assert await app.membership.flush() == 0


async def test_adapter_outage_keeps_both_verified_group_and_speaker(app):
    lifecycle = LifecycleStream(app.redis, app.bot, shards=2, reclaim_ms=0)
    offline_groups = GroupService(app.groups.repository, unavailable, retry_queue=lifecycle)
    offline_groups.repository = SimpleNamespace(get=AsyncMock(side_effect=OSError("test outage")))
    offline_membership = MembershipService(
        SimpleNamespace(known_active=AsyncMock(side_effect=OSError("test outage"))),
        unavailable,
        app.stream,
    )
    entity = types.Chat(77, "New group", types.ChatPhotoEmpty(), 1, app.now, 1)
    event = SimpleNamespace(
        is_group=True,
        chat_id=-77,
        chat=entity,
        id=1,
        date=app.now,
        sender_id=7,
        sender=types.User(id=7, first_name="Speaker"),
    )
    tracker = GroupTracker(None, offline_groups, 123)
    members = MembershipTracker(None, offline_groups, tracker, offline_membership, 123)
    await tracker.observe(event)
    await members.observe(event)
    recovered_groups = GroupService(app.groups.repository, app.factory, retry_queue=lifecycle)
    app.membership.recover_groups = recovered_groups.flush_deferred
    app.membership.lifecycle_stream = lifecycle
    assert await app.membership.flush() == 1
    assert (await app.groups.get(-77)).title == "New group"
    assert await app.pool.fetchval("SELECT is_active FROM group_memberships WHERE chat_id=-77")
    assert await app.pool.fetchval("SELECT count(*) FROM membership_events WHERE chat_id=-77") == 1
    assert await app.membership.flush() == 0


async def test_old_profile_replay_preserves_new_metadata_block_ban_and_role(app):
    old = HumanProfile(7, None, "Old", None, app.now - timedelta(seconds=10))
    await app.membership.profile(7, None, "Current", None)
    await app.pool.execute(
        "UPDATE bot_users SET is_blocked=TRUE,is_banned=TRUE,is_admin=TRUE,updated_at=now()"
    )
    await app.stream.append(old)
    await app.membership.flush()
    row = await app.pool.fetchrow("SELECT * FROM bot_users")
    assert (
        row["first_name"] == "Current"
        and row["is_blocked"]
        and row["is_banned"]
        and row["is_admin"]
    )
    assert not row["is_started"]


@pytest.mark.parametrize("topic", ["membership", "lifecycle"])
@pytest.mark.parametrize("poison", ["recursive", "nul", "shape"])
async def test_poison_records_are_quarantined_without_blocking_valid_work(app, topic, poison):
    lifecycle = LifecycleStream(app.redis, app.bot, shards=2, reclaim_ms=0)
    groups = GroupService(app.groups.repository, app.factory, retry_queue=lifecycle)
    queue = app.stream if topic == "membership" else lifecycle
    event = (
        MembershipEvent(-10, 7, "healthy", "message", app.now)
        if topic == "membership"
        else GroupChange(-10, "observed", "healthy", app.now, "group")
    )
    if poison == "recursive":
        raw = "[" * 1500 + "0" + "]" * 1500
    elif poison == "nul":
        data = event.dump()
        data["first_name" if topic == "membership" else "title"] = "bad\x00value"
        raw = json.dumps(data)
    else:
        raw = '{"occurred_at":{}}'
    shard = queue.shard(-10)
    await app.redis.xadd(queue.key(shard), {"event": raw})
    await queue.append(event)
    if topic == "membership":
        assert await app.membership.flush() == 1
    else:
        await groups.flush_deferred()
        assert (
            await app.pool.fetchval("SELECT count(*) FROM group_events WHERE event_key='healthy'")
            == 1
        )
    assert await app.redis.xlen(queue.key(shard)) == 0
    invalid = await app.redis.xrange(queue.invalid_key(shard))
    assert len(invalid) == 1 and b"event" not in invalid[0][1]


async def test_recursive_cache_repairs_from_sql(app):
    cache = ObjectCache(app.redis, app.bot)
    from repositories.redis.keys import object_key

    key = object_key(app.bot, "group", -10)
    await app.redis.set(key, "[" * 1500 + "0" + "]" * 1500)
    result = await cache.read("group", -10, lambda: app.groups.get(-10))
    assert result.chat_id == -10
    assert json.loads(await app.redis.get(key))["value"]["chat_id"] == -10


async def test_profile_fails_visibly_if_sql_and_durable_state_are_both_unavailable():
    stream = SimpleNamespace(append=AsyncMock(side_effect=OSError("STATE unavailable")))
    service = MembershipService(None, unavailable, stream)
    with pytest.raises(OSError, match="STATE unavailable"):
        await service.profile(7, None, "Human", None)


@pytest.mark.parametrize("batched", [False, True])
async def test_older_group_activity_does_not_replace_fresh_private_profile(app, batched):
    await app.membership.persist(MembershipEvent(-10, 7, "seed", "message", app.now))
    await app.membership.profile(7, "new_name", "Current", None)
    event = MembershipEvent(
        -10,
        7,
        "old",
        "message",
        app.now - timedelta(seconds=1),
        username="old_name",
        first_name="Old",
    )
    if batched:
        await app.stream.append(event)
        await app.membership.flush()
    else:
        await app.membership.persist(event)
    row = await app.pool.fetchrow("SELECT username,first_name FROM bot_users")
    assert row["username"] == "new_name" and row["first_name"] == "Current"
    assert await app.pool.fetchval("SELECT message_count FROM group_memberships") == 2


async def test_missing_parent_is_recreated_even_with_recent_observation_cache(app):
    import time

    entity = types.Chat(77, "Group", types.ChatPhotoEmpty(), 1, app.now, 1)
    tracker = GroupTracker(None, app.groups, 123)
    tracker._seen[-77] = time.monotonic() + 60
    members = MembershipTracker(None, app.groups, tracker, app.membership, 123)
    await members.observe(
        SimpleNamespace(
            is_group=True,
            chat_id=-77,
            chat=entity,
            id=1,
            date=app.now,
            sender_id=7,
            sender=types.User(id=7),
        )
    )
    assert await app.pool.fetchval("SELECT count(*) FROM group_memberships WHERE chat_id=-77") == 1
