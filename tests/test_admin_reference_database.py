"""Real SQL/Redis evidence; Telegram effects remain explicitly simulated."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from db.postgres.engine import migrate
from db.redis.pool import create_redis
from repositories.postgres.admin import AdminRepository
from repositories.postgres.group import GroupRepository
from repositories.postgres.membership import MembershipRepository
from repositories.postgres.outbox import OutboxRepository
from repositories.postgres.unit_of_work import PostgresUnitOfWork
from repositories.postgres.workflow import WorkflowRepository
from repositories.redis.activity import ActivityStream
from repositories.redis.keys import activity_key, invalid_activity_key, object_key
from repositories.redis.objects import ObjectCache
from schemas.group import GroupChange
from schemas.media import DownloadError
from schemas.membership import MembershipEvent
from services.admin import AdminService
from services.group import GroupService
from services.membership import MembershipService
from services.operations import OperationsService


@pytest.fixture
async def app():
    dsn, redis_url = os.getenv("TEST_DATABASE_URL"), os.getenv("TEST_REDIS_URL")
    if not dsn or not redis_url:
        pytest.skip("Dedicated PostgreSQL and Redis are required")
    schema = "admin_test_" + uuid.uuid4().hex
    owner = await asyncpg.connect(dsn)
    await owner.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=12, server_settings={"search_path": schema}
    )
    redis = create_redis(redis_url)
    bot = uuid.uuid4().int % (2**62)

    def factory():
        return PostgresUnitOfWork(pool, bot)

    await migrate(pool)
    groups = GroupService(GroupRepository(pool, bot), factory)
    admin = AdminService(AdminRepository(pool, bot), factory, groups, 999)
    stream = ActivityStream(redis, bot, shards=2, batch_size=100, reclaim_ms=0)
    membership = MembershipService(MembershipRepository(pool, bot), factory, stream)
    now = datetime.now(UTC)
    await groups.record(GroupChange(-10, "added", "seed", now, "group", active=True, admin=False))
    value = SimpleNamespace(
        pool=pool,
        redis=redis,
        bot=bot,
        factory=factory,
        groups=groups,
        admin=admin,
        stream=stream,
        membership=membership,
        now=now,
        workflow=WorkflowRepository(pool, bot),
    )
    try:
        yield value
    finally:
        keys = []
        async for key in redis.scan_iter(match=f"*:{bot}:*"):
            keys.append(key)
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        await pool.close()
        await owner.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await owner.close()


def member(app, kind="message", key="one", seconds=1, sequence=1, stream="message"):
    return MembershipEvent(
        -10,
        7,
        key,
        kind,
        app.now + timedelta(seconds=seconds),
        sequence,
        "sample",
        "Human",
        None,
        stream,
    )


async def test_thousand_replayed_membership_messages_count_exactly_once(app):
    event = member(app)
    await asyncio.gather(*(app.membership.persist(event) for _ in range(1000)))
    assert await app.pool.fetchval("SELECT count(*) FROM group_memberships") == 1
    assert await app.pool.fetchval("SELECT message_count FROM group_memberships") == 1
    assert await app.pool.fetchval("SELECT count(*) FROM membership_events") == 1
    assert await app.pool.fetchval("SELECT last_activity_at FROM bot_groups") == event.occurred_at


async def test_old_activity_cannot_resurrect_leave_and_new_message_can(app):
    await app.membership.persist(member(app))
    await app.membership.persist(member(app, "left", "left", 5, 10, "basic"))
    await app.membership.persist(member(app, key="old", seconds=2, sequence=200))
    await app.membership.persist(member(app, key="same-second", seconds=5, sequence=300))
    assert not await app.pool.fetchval("SELECT is_active FROM group_memberships")
    await app.membership.persist(member(app, key="new", seconds=6))
    assert await app.pool.fetchval("SELECT is_active FROM group_memberships")
    assert await app.pool.fetchval("SELECT message_count FROM group_memberships") == 4


async def test_late_basic_event_with_smaller_version_cannot_reverse_new_join(app):
    await app.membership.persist(member(app, "joined", "join", 1, 3, "basic"))
    await app.membership.persist(member(app, "left", "late-old-delete", 10, 2, "basic"))
    assert await app.pool.fetchval("SELECT is_active FROM group_memberships")
    assert await app.pool.fetchval("SELECT state_event_order FROM group_memberships") == 3


async def test_reclaimed_activity_after_commit_before_ack_does_not_double_count(app, monkeypatch):
    await app.membership.persist(member(app))
    await app.membership.observe(member(app, key="queued", seconds=2))
    original = app.stream.acknowledge
    monkeypatch.setattr(
        app.stream, "acknowledge", AsyncMock(side_effect=OSError("ack unavailable"))
    )
    with pytest.raises(OSError):
        await app.membership.flush()
    assert await app.pool.fetchval("SELECT message_count FROM group_memberships") == 2
    monkeypatch.setattr(app.stream, "acknowledge", original)
    replacement = MembershipService(app.membership.repository, app.factory, app.stream)
    await replacement.flush()
    assert await app.pool.fetchval("SELECT message_count FROM group_memberships") == 2
    assert await app.redis.xlen(activity_key(app.bot, app.stream.shard(-10))) == 0


async def test_poison_record_is_not_acknowledged_when_quarantine_fails(app, monkeypatch):
    shard = app.stream.shard(-10)
    key = activity_key(app.bot, shard)
    await app.redis.xadd(key, {"event": "invalid json"})
    original = app.stream.quarantine
    monkeypatch.setattr(
        app.stream, "quarantine", AsyncMock(side_effect=OSError("quarantine unavailable"))
    )
    with pytest.raises(OSError):
        await app.membership.flush()
    assert await app.redis.xlen(key) == 1
    monkeypatch.setattr(app.stream, "quarantine", original)
    await app.membership.flush()
    assert await app.redis.xlen(key) == 0
    assert await app.redis.xlen(invalid_activity_key(app.bot, shard)) == 1


async def test_stream_outage_falls_back_to_immediate_sql(app, monkeypatch):
    await app.membership.persist(member(app))
    monkeypatch.setattr(app.stream, "append", AsyncMock(side_effect=OSError("offline")))
    await app.membership.observe(member(app, key="fallback"))
    assert await app.pool.fetchval("SELECT message_count FROM group_memberships") == 2


async def test_roles_are_fresh_main_admin_is_protected_and_other_bot_is_isolated(app):
    with pytest.raises(DownloadError):
        await app.admin.authorize(7)
    await app.admin.mutate(999, "grant", 7)
    assert await app.admin.authorize(7) is False
    with pytest.raises(DownloadError):
        await app.admin.mutate(7, "grant", 8)
    with pytest.raises(DownloadError):
        await app.admin.mutate(999, "ban", 999)
    other = AdminService(AdminRepository(app.pool, app.bot + 1), app.factory, app.groups, 1000)
    with pytest.raises(DownloadError):
        await other.authorize(7)
    await app.admin.mutate(999, "revoke", 7)
    with pytest.raises(DownloadError):
        await app.admin.authorize(7)
    assert await app.pool.fetchval("SELECT count(*) FROM admin_actions") == 2


async def test_confirmation_is_owner_scoped_expiring_and_single_use_under_race(app):
    token = await app.admin.prepare(999, "health", {})
    with pytest.raises(DownloadError):
        await app.admin.confirm(7, token)
    results = await asyncio.gather(
        *(app.admin.confirm(999, token) for _ in range(50)), return_exceptions=True
    )
    assert sum(type(result) is int for result in results) == 1
    assert await app.pool.fetchval("SELECT count(*) FROM admin_jobs") == 1
    expired = await app.admin.prepare(999, "health", {})
    await app.pool.execute(
        "UPDATE admin_requests SET expires_at=now()-interval '1 minute' WHERE token=$1", expired
    )
    with pytest.raises(DownloadError):
        await app.admin.confirm(999, expired)


async def test_revoked_delegate_cannot_confirm_old_health_button(app):
    await app.admin.mutate(999, "grant", 7)
    token = await app.admin.prepare(7, "health", {})
    await app.admin.mutate(999, "revoke", 7)
    with pytest.raises(DownloadError):
        await app.admin.confirm(7, token)
    assert await app.pool.fetchval("SELECT count(*) FROM admin_jobs") == 0


async def test_job_enumeration_private_audience_exclusions_and_resume(app):
    for identity in (1, 2, 3, 4):
        await app.workflow.touch_user(identity, started=identity != 2)
    await app.admin.mutate(999, "ban", 3)
    token = await app.admin.prepare(
        999, "broadcast", {"audience": "users", "text": "Test message", "exclude": [4]}
    )
    identity = await app.admin.confirm(999, token)
    sent = []
    delivery = SimpleNamespace(
        deliver=AsyncMock(side_effect=lambda effect: sent.append(effect.target_id))
    )
    worker = OperationsService(
        OutboxRepository(app.pool, app.bot), app.factory, app.admin, delivery
    )
    for _ in range(3):
        await worker.tick()
        await app.pool.execute("UPDATE admin_jobs SET available_at=now()")
    assert sent == [1]
    row = await app.pool.fetchrow("SELECT * FROM admin_jobs WHERE id=$1", identity)
    assert (
        row["status"] == "done"
        and row["success_count"] == 1
        and row["skipped_count"] == 1
        and row["total_count"] == 2
    )


async def test_outbox_claims_do_not_overlap_and_expired_owner_cannot_finish(app):
    repo = OutboxRepository(app.pool, app.bot)
    for index in range(20):
        await repo.enqueue(str(index), "message", 999, {"text": "Test"})
    batches = await asyncio.gather(*(repo.claim(str(index), 5) for index in range(4)))
    assert len({effect.id for batch in batches for effect in batch}) == 20
    effect = batches[0][0]
    await app.pool.execute(
        "UPDATE outbox_events SET locked_until=now()-interval '1 minute' WHERE id=$1", effect.id
    )
    reclaimed = (await repo.claim("replacement"))[0]
    assert reclaimed.id == effect.id and reclaimed.random_id == effect.random_id
    async with app.factory() as uow:
        assert not await uow.outbox.finish(effect.id, "0", "sent")
        assert await uow.outbox.finish(effect.id, "replacement", "sent")


async def test_cancelled_job_does_not_send_already_enumerated_work(app):
    await app.workflow.touch_user(1, started=True)
    identity = await app.admin.confirm(
        999, await app.admin.prepare(999, "broadcast", {"audience": "users", "text": "Test"})
    )
    delivery = SimpleNamespace(deliver=AsyncMock())
    worker = OperationsService(
        OutboxRepository(app.pool, app.bot), app.factory, app.admin, delivery
    )
    await worker.enumerate()
    assert await app.admin.cancel(999, identity)
    await worker.tick()
    delivery.deliver.assert_not_awaited()
    assert await app.pool.fetchval("SELECT status FROM admin_jobs") == "cancelled"


async def test_lifecycle_notification_is_transactional_and_replay_safe(app):
    app.groups.main_admin_id = 999
    change = GroupChange(-20, "added", "one", datetime.now(UTC), "group", active=True, admin=False)
    await asyncio.gather(*(app.groups.record(change) for _ in range(20)))
    assert await app.pool.fetchval("SELECT count(*) FROM outbox_events") == 1
    assert await app.pool.fetchval("SELECT count(*) FROM group_events WHERE chat_id=-20") == 1


async def test_group_custom_limit_and_migration_metadata_are_preserved(app):
    await app.admin.mutate(999, "limit", -10, 2)
    await app.groups.metadata(-10, 7, [7, 8], 23, "https://t.me/example")
    await app.groups.claim_link(1, -10, "first", 60)
    await app.groups.claim_link(2, -10, "second", 60)
    with pytest.raises(DownloadError, match="گروه"):
        await app.groups.claim_link(3, -10, "third", 60)
    await app.groups.migrate(-10, -1000000000077, app.now + timedelta(seconds=2))
    new = await app.groups.get(-1000000000077)
    assert new.links_per_minute == 2 and new.owner_id == 7 and new.admin_ids == (7, 8)
    assert new.added_at == (await app.groups.get(-10)).added_at


async def test_corrupted_group_cache_is_reconstructed_and_negative_result_is_cached(app):
    cache = ObjectCache(app.redis, app.bot)
    key = object_key(app.bot, "group", -10)
    await app.redis.set(key, '{"version":1,"value":{"chat_id":-10}}')

    # AsyncMock awaits a callable's return only when that callable itself is async.
    async def loader():
        return await app.groups.repository.get(-10)

    first = await cache.read("group", -10, loader)
    assert first.chat_id == -10
    assert (
        await cache.read("group", -10, AsyncMock(side_effect=AssertionError("cache miss"))) == first
    )
    missing = AsyncMock(return_value=None)
    assert await cache.read("group", -11, missing) is None
    assert await cache.read("group", -11, missing) is None
    missing.assert_awaited_once()


async def test_lifecycle_sql_outage_defers_then_recovers_without_false_commit(app):
    from contextlib import asynccontextmanager

    from repositories.redis.activity import LifecycleStream
    from repositories.redis.keys import lifecycle_key

    @asynccontextmanager
    async def unavailable():
        raise OSError("SQL temporarily offline")
        yield

    queue = LifecycleStream(app.redis, app.bot, shards=2, reclaim_ms=0)
    service = GroupService(app.groups.repository, unavailable, retry_queue=queue)
    change = GroupChange(
        -10,
        "removed",
        "deferred",
        app.now + timedelta(seconds=2),
        "group",
        active=False,
        admin=False,
    )
    assert await service.record(change) is None
    assert (await app.groups.get(-10)).is_active is True
    assert await app.redis.xlen(lifecycle_key(app.bot, queue.shard(-10))) == 1
    service._uow_factory = app.factory
    await service.flush_deferred()
    assert (await app.groups.get(-10)).is_active is False
    assert await app.redis.xlen(lifecycle_key(app.bot, queue.shard(-10))) == 0
    assert (
        await app.pool.fetchval("SELECT count(*) FROM group_events WHERE event_key='deferred'") == 1
    )


@pytest.mark.parametrize("raw", ["1", "[]", '{"version":true}', b"\xff"])
async def test_scalar_and_invalid_cache_objects_are_actually_repaired(app, raw):
    cache = ObjectCache(app.redis, app.bot)
    key = object_key(app.bot, "group", -10)
    await app.redis.set(key, raw)
    result = await cache.read("group", -10, lambda: app.groups.repository.get(-10))
    cached = await app.redis.get(key)
    assert cache.decode(cached, "group", -10) == result
    assert key not in cache.dirty


async def test_wrongtype_cache_is_only_a_miss_and_group_revision_cannot_regress(app):
    cache = ObjectCache(app.redis, app.bot)
    key = object_key(app.bot, "group", -10)
    await app.redis.sadd(key, "wrong type")
    old = await cache.read("group", -10, lambda: app.groups.repository.get(-10))
    await app.groups.record(
        GroupChange(
            -10,
            "promoted",
            "new-admin",
            app.now + timedelta(seconds=2),
            "group",
            active=True,
            admin=True,
        )
    )
    fresh = await app.groups.get(-10)
    await cache.put("group", -10, fresh)
    await cache.put("group", -10, old)
    assert (
        await cache.read("group", -10, AsyncMock(side_effect=AssertionError("cache miss")))
    ).revision == fresh.revision
    assert cache.retry_at == 0


async def test_failed_write_through_keeps_sql_and_repairs_stale_cached_snapshot(app, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    cache = ObjectCache(app.redis, app.bot)
    app.groups.cache = cache
    old = await app.groups.get(-10)
    original = app.redis.eval
    monkeypatch.setattr(app.redis, "eval", AsyncMock(side_effect=RedisConnectionError("offline")))
    result = await app.groups.record(
        GroupChange(
            -10,
            "promoted",
            "new-admin",
            app.now + timedelta(seconds=2),
            "group",
            active=True,
            admin=True,
        )
    )
    assert result["applied"] and await app.pool.fetchval("SELECT is_admin FROM bot_groups")
    assert cache.dirty
    monkeypatch.setattr(app.redis, "eval", original)
    cache.retry_at = 0
    repaired = await app.groups.get(-10)
    assert repaired.is_admin and repaired.revision > old.revision and not cache.dirty


async def test_user_and_setting_cache_do_not_authorize_or_hide_live_policy_changes(app):
    cache = ObjectCache(app.redis, app.bot)
    app.admin.cache = cache
    await app.workflow.touch_user(7)
    assert not (await app.admin.user(999, 7)).is_admin
    await app.admin.mutate(999, "grant", 7)
    assert (await app.admin.user(999, 7)).is_admin
    assert (await app.admin.options(999))["downloads_enabled"]
    await app.admin.mutate(999, "pause", value=False)
    assert not (await app.admin.options(999))["downloads_enabled"]
    with pytest.raises(DownloadError, match="نگهداری"):
        await app.admin.check_download(7)
    await app.admin.mutate(999, "revoke", 7)
    with pytest.raises(DownloadError):
        await app.admin.authorize(7)


async def test_csv_import_cannot_forge_start_roles_or_human_memberships(app):
    bad = b"user_id,is_admin\n7,true\n"
    with pytest.raises(DownloadError):
        await app.admin.import_users(999, bad)
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 0
    assert (
        await app.admin.import_users(999, b"user_id,username,first_name\n7,name,=Dangerous\n") == 1
    )
    row = await app.pool.fetchrow("SELECT * FROM bot_users WHERE user_id=7")
    assert not row["is_started"] and not row["is_admin"] and row["account_kind"] == "unknown"
    assert await app.pool.fetchval("SELECT count(*) FROM group_memberships") == 0
    exported = await app.admin.export(999, "users")
    assert "'=Dangerous" in exported.data.decode("utf-8-sig")
    with pytest.raises(DownloadError):
        await app.admin.import_users(7, b"user_id\n8\n")


async def test_group_peer_backup_is_bot_scoped_and_never_changes_standing(app):
    await app.groups.remember_peer(-10, 10, "group", None)
    peer = await app.groups.peer(-10)
    assert peer.chat_kind == "group" and peer.raw_id == 10
    assert await GroupRepository(app.pool, app.bot + 1).peer(-10) is None
    with pytest.raises(ValueError):
        await app.groups.remember_peer(-10, 20, "group", None)
    assert (await app.groups.get(-10)).is_active


async def test_flood_wait_preserves_due_time_and_retry_random_id(app):
    from schemas.admin import RetryEffect

    identity = await app.admin.confirm(
        999, await app.admin.prepare(999, "health", {"chat_id": -10})
    )
    delivery = SimpleNamespace(deliver=AsyncMock(side_effect=RetryEffect(200, "flood_wait")))
    repo = OutboxRepository(app.pool, app.bot)
    worker = OperationsService(repo, app.factory, app.admin, delivery)
    await worker.enumerate()
    first = (await repo.claim(worker.owner))[0]
    await worker.execute(first)
    row = await app.pool.fetchrow("SELECT status,available_at FROM outbox_events")
    assert row["status"] == "pending" and row["available_at"] > datetime.now(UTC) + timedelta(
        seconds=190
    )
    assert (
        await app.pool.fetchval("SELECT success_count FROM admin_jobs WHERE id=$1", identity) == 0
    )
    await app.pool.execute("UPDATE outbox_events SET available_at=now()")
    replacement = (await repo.claim("replacement"))[0]
    assert replacement.random_id == first.random_id


@pytest.mark.parametrize(
    "content",
    [
        b"user_id,user_id\n7,8\n",
        b"user_id,username\n7,name,extra\n",
        b"user_id,username\n7\n",
        b"user_id,username\n7,null\x00byte\n",
    ],
)
async def test_csv_rejects_ambiguous_or_unrepresentable_rows_atomically(app, content):
    with pytest.raises(DownloadError):
        await app.admin.import_users(999, content)
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 0


async def test_shared_publication_budget_and_pause_span_workers(app):
    from repositories.redis.telegram_rate import TelegramRateRepository

    one = TelegramRateRepository(app.redis, app.bot)
    two = TelegramRateRepository(app.redis, app.bot)
    results = await asyncio.gather(*(client.reserve(1) for client in (one, two, one, two)))
    assert sum(value == 0 for value in results) == 1
    await one.pause(20)
    assert await two.reserve(1) > 19
    await two.pause(1)
    assert await one.reserve(1) > 19


async def test_same_second_service_join_cannot_override_explicit_participant_leave(app):
    await app.membership.persist(member(app, "left", "raw-left", 5, 2, "participant"))
    await app.membership.persist(member(app, "joined", "service-join", 5, 999, "service"))
    assert not await app.pool.fetchval("SELECT is_active FROM group_memberships")
    await app.membership.persist(member(app, "joined", "new-service-join", 6, 1000, "service"))
    assert await app.pool.fetchval("SELECT is_active FROM group_memberships")


async def test_revision_nine_upgrade_keeps_existing_group_and_user_identities(app):
    from db.postgres.migrations import MIGRATIONS

    schema = "upgrade_test_" + uuid.uuid4().hex
    await app.pool.execute(f'CREATE SCHEMA "{schema}"')
    async with app.pool.acquire() as connection:
        await connection.execute(f'SET search_path TO "{schema}"')
        try:
            from db.postgres.schema import LEDGER_SCHEMA

            await connection.execute(LEDGER_SCHEMA)
            for version, name, sql in MIGRATIONS[:9]:
                await connection.execute(sql)
                await connection.execute(
                    "INSERT INTO app_schema_migrations(version,name) VALUES($1,$2)", version, name
                )
            await connection.execute(
                "INSERT INTO bot_users(telegram_account_id,user_id,is_started) VALUES($1,7,TRUE)",
                app.bot,
            )
            await connection.execute(
                "INSERT INTO bot_groups(telegram_account_id,chat_id,chat_kind,is_active,is_admin,added_by_user_id) VALUES($1,-10,'group',TRUE,TRUE,7)",
                app.bot,
            )
            async with asyncpg.create_pool(
                os.environ["TEST_DATABASE_URL"],
                min_size=1,
                max_size=3,
                server_settings={"search_path": schema},
            ) as upgrade_pool:
                await asyncio.gather(*(migrate(upgrade_pool) for _ in range(3)))
            user = await connection.fetchrow("SELECT * FROM bot_users")
            group = await connection.fetchrow("SELECT * FROM bot_groups")
            assert user["user_id"] == 7 and user["is_started"] and not user["is_admin"]
            assert group["chat_id"] == -10 and group["is_admin"] and group["added_by_user_id"] == 7
            assert await connection.fetchval("SELECT max(version) FROM app_schema_migrations") == 10
        finally:
            await connection.execute("RESET search_path")
            await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
