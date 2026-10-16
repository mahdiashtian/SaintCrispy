"""Durable departures and exclusive maintenance, without a live Telegram account."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_admin_reference_database import app as app

from core.maintenance import MaintenanceGate
from schemas.membership import MembershipEvent
from services.membership import MembershipService


async def test_sql_outage_defers_join_message_and_leave_then_replays_once(app):
    @asynccontextmanager
    async def unavailable():
        raise OSError("database unavailable")
        yield

    repository = SimpleNamespace(known_active=AsyncMock(side_effect=OSError("offline")))
    service = MembershipService(repository, unavailable, app.stream)
    events = [
        MembershipEvent(-10, 7, "join", "joined", app.now, stream="participant"),
        MembershipEvent(-10, 7, "message", "message", app.now + timedelta(seconds=1)),
        MembershipEvent(
            -10, 7, "leave", "left", app.now + timedelta(seconds=2), stream="participant"
        ),
    ]
    for event in events:
        assert await service.observe(event) is False
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 0
    await app.membership.flush()
    row = await app.pool.fetchrow("SELECT * FROM group_memberships")
    assert row["is_active"] is False and row["message_count"] == 1
    assert await app.pool.fetchval("SELECT count(*) FROM membership_events") == 3
    assert await app.pool.fetchval("SELECT count(*) FROM membership_departures") == 1
    assert await app.membership.flush() == 0


async def test_unknown_departure_fences_older_buffered_message_without_inventing_a_user(app):
    left = MembershipEvent(-10, 7, "left", "left", app.now, stream="participant")
    assert not await app.membership.persist(left)
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 0
    assert await app.pool.fetchval("SELECT count(*) FROM membership_departures") == 1
    await app.membership.persist(
        MembershipEvent(-10, 7, "old-message", "message", app.now - timedelta(seconds=5))
    )
    assert await app.pool.fetchval("SELECT is_active FROM group_memberships") is False
    await app.membership.persist(
        MembershipEvent(-10, 7, "new-message", "message", app.now + timedelta(seconds=5))
    )
    assert await app.pool.fetchval("SELECT is_active FROM group_memberships") is True


@pytest.mark.parametrize("field", ["event_key", "first_name"])
def test_membership_rejects_postgres_poison_before_stream_append(field):
    from datetime import UTC, datetime

    values = dict(
        chat_id=-10, user_id=7, event_key="event", kind="left", occurred_at=datetime.now(UTC)
    )
    values[field] = "bad\x00value"
    with pytest.raises(ValueError):
        MembershipEvent(**values)


def resources():
    worker = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    return dict(
        jobs=SimpleNamespace(count=0),
        workers=[worker],
        memberships=SimpleNamespace(active={}, stream=None, flush=AsyncMock()),
        groups=SimpleNamespace(retry_queue=None, flush_deferred=AsyncMock()),
        tracker=SimpleNamespace(_seen={}, _checked={}),
        cache=None,
        bot_id=7,
    )


async def test_two_overlapping_admin_confirmations_do_not_deadlock_and_resume_updates():
    gate, deps = MaintenanceGate(), resources()
    gate.ready = True
    first_entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    workers_stopped = asyncio.Event()
    deps["workers"][0].__aexit__.side_effect = lambda *args: workers_stopped.set()
    order = []

    async def first():
        async with gate.operation(), gate.guard(**deps):
            order.append("first")
            first_entered.set()
            await release.wait()

    async def second():
        async with gate.operation():
            second_started.set()
            await workers_stopped.wait()
            async with gate.guard(**deps):
                order.append("second")

    second_task = asyncio.create_task(second())
    await second_started.wait()
    first_task = asyncio.create_task(first())
    await asyncio.wait_for(first_entered.wait(), 2)
    release.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), 3)
    assert order == ["first", "second"] and not gate.paused and not gate.active
    assert deps["workers"][0].__aenter__.await_count == 2


async def test_maintenance_failure_always_restarts_workers_and_unpauses():
    gate, deps = MaintenanceGate(), resources()
    gate.ready = True
    with pytest.raises(RuntimeError):
        async with gate.guard(**deps):
            raise RuntimeError("restore rolled back")
    assert not gate.paused
    deps["workers"][0].__aenter__.assert_awaited_once()
    async with gate.operation():
        assert len(gate.active) == 1


async def test_maintenance_invalidates_private_profile_observation_cache():
    gate, deps = MaintenanceGate(), resources()
    gate.ready = True
    deps["memberships"].profile_seen = {7: 100000000000}
    async with gate.guard(**deps):
        pass
    assert not deps["memberships"].profile_seen


async def test_maintenance_waits_for_startup_readiness():
    from schemas.media import DownloadError

    gate, deps = MaintenanceGate(), resources()
    with pytest.raises(DownloadError):
        async with gate.guard(**deps):
            pytest.fail("maintenance entered before catch-up and worker startup")
    deps["workers"][0].__aexit__.assert_not_awaited()


async def test_partial_stop_failure_restarts_attempted_worker_and_unpauses():
    gate, deps = MaintenanceGate(), resources()
    gate.ready = True
    deps["workers"][0].__aexit__.side_effect = RuntimeError("stop failed")
    with pytest.raises(RuntimeError, match="stop failed"):
        async with gate.guard(**deps):
            pytest.fail("unsafe maintenance entered")
    deps["workers"][0].__aenter__.assert_awaited_once()
    assert not gate.paused


async def test_one_restart_failure_does_not_strand_other_workers():
    gate, deps = MaintenanceGate(), resources()
    gate.ready = True
    second = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    deps["workers"].append(second)
    deps["workers"][0].__aenter__.side_effect = RuntimeError("restart failed")
    with pytest.raises(RuntimeError, match="worker restart failed"):
        async with gate.guard(**deps):
            pass
    second.__aenter__.assert_awaited_once()
    assert not gate.paused


async def test_recovery_commits_deferred_group_before_its_queued_member(app):
    from repositories.redis.activity import LifecycleStream
    from schemas.group import GroupChange
    from services.group import GroupService

    lifecycle = LifecycleStream(app.redis, app.bot, shards=2, reclaim_ms=0)
    groups = GroupService(app.groups.repository, app.factory, retry_queue=lifecycle)
    await lifecycle.append(
        GroupChange(-77, "added", "delayed-group", app.now, "group", active=True)
    )
    await app.stream.append(MembershipEvent(-77, 7, "queued-message", "message", app.now))
    app.membership.recover_groups = groups.flush_deferred
    await app.membership.flush()
    assert await app.pool.fetchval("SELECT is_active FROM group_memberships WHERE chat_id=-77")


async def test_member_event_waits_for_group_creation_already_claimed_by_another_worker(app):
    from repositories.redis.activity import LifecycleStream
    from schemas.group import GroupChange
    from services.group import GroupService

    lifecycle = LifecycleStream(app.redis, app.bot, shards=2, reclaim_ms=120000)
    groups = GroupService(app.groups.repository, app.factory, retry_queue=lifecycle)
    change = GroupChange(-77, "added", "inflight-group", app.now, "group", active=True)
    identity = await lifecycle.append(change)
    shard = lifecycle.shard(-77)
    assert await lifecycle.batch(shard, "other-worker")
    app.membership.recover_groups = groups.flush_deferred
    app.membership.lifecycle_stream = lifecycle
    event = MembershipEvent(-77, 7, "queued-message", "message", app.now)
    assert await app.membership.persist(event) is False
    assert await app.membership.flush() == 0
    assert await app.redis.xlen(app.stream.key(app.stream.shard(-77))) == 1
    await groups.record(change)
    await lifecycle.acknowledge(shard, [identity])
    assert await app.membership.flush() == 1
    assert await app.redis.xlen(app.stream.key(app.stream.shard(-77))) == 0
