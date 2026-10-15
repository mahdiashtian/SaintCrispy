"""Real PostgreSQL lifecycle replay, transactional quota and migration tests."""

import asyncio
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from db.postgres.engine import migrate
from repositories.postgres.group import GroupRepository
from repositories.postgres.unit_of_work import PostgresUnitOfWork
from repositories.postgres.workflow import WorkflowRepository
from schemas.group import GroupChange
from schemas.media import DownloadError
from services.group import GroupService
from services.limits import RequestLimiter

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def change(kind="added", *, chat=-10, sequence=1, active=True, admin=False, **values):
    return GroupChange(
        chat,
        kind,
        f"{kind}:{sequence}",
        NOW + timedelta(seconds=sequence),
        "group",
        active=active,
        admin=admin,
        stream="basic",
        sequence=sequence,
        priority=2,
        **values,
    )


@pytest.fixture
async def database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for real group integration")
    schema = "groups_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        url, min_size=1, max_size=12, server_settings={"search_path": schema}
    )
    try:
        await migrate(pool)

        def factory():
            return PostgresUnitOfWork(pool, 123)

        service = GroupService(GroupRepository(pool, 123), factory)
        yield pool, service, factory
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_lifecycle_replay_and_out_of_order_evidence_preserve_current_standing(database):
    pool, service, _ = database
    joined = change(actor_id=50, title="Test group")
    results = await asyncio.gather(*(service.record(joined) for _ in range(100)))
    assert sum(result is not None for result in results) == 1
    await service.record(change("promoted", sequence=3, admin=True))
    await service.record(change("removed", sequence=2, active=False, admin=False))
    row = await service.get(-10)
    assert row.is_active and row.is_admin and row.added_by_user_id == 50
    assert not await pool.fetchval("SELECT applied FROM group_events WHERE kind='removed'")
    await service.record(change("demoted", sequence=4))
    row = await service.get(-10)
    assert row.is_active and not row.is_admin and row.admin_since is None
    await service.record(change("removed", sequence=5, active=False))
    await service.record(change("added", sequence=6, admin=None, actor_id=51))
    row = await service.get(-10)
    assert row.is_active and row.is_admin is False and row.can_send is None
    assert row.readded_at and row.removed_at and row.added_by_user_id == 50
    assert await pool.fetchval("SELECT count(*) FROM group_events") == 6
    assert await pool.fetchval("SELECT count(*) FROM bot_groups") == 1


async def test_same_second_weaker_service_and_unknown_observation_do_not_demote(database):
    pool, service, _ = database
    promoted = change("promoted", sequence=5, admin=True)
    await service.record(promoted)
    await service.record(
        replace(
            promoted,
            kind="added",
            event_key="service:7",
            stream="service",
            sequence=7,
            priority=1,
            admin=None,
        )
    )
    assert (await service.get(-10)).is_admin is True
    await service.record(change("removed", sequence=8, active=False))
    await service.record(
        replace(
            change(sequence=9),
            kind="observed",
            event_key="seen:1",
            admin=None,
            priority=0,
            stream="observation",
            sequence=0,
        )
    )
    assert (await service.get(-10)).is_active is False
    assert await pool.fetchval("SELECT count(*) FROM group_events") == 4


async def test_basic_version_orders_updates_without_server_dates(database):
    _, service, _ = database
    first = change("promoted", sequence=10, admin=True)
    await service.record(first)
    # Delivery timestamps of undated basic-chat updates are not their version order.
    await service.record(
        replace(
            change("demoted", sequence=11), occurred_at=first.occurred_at - timedelta(seconds=3)
        )
    )
    assert (await service.get(-10)).is_admin is False
    await service.record(
        replace(first, event_key="replayed-old", occurred_at=NOW + timedelta(days=1))
    )
    assert (await service.get(-10)).is_admin is False


async def test_atomic_thousand_group_arrivals_admit_exactly_five_and_survive_restart(database):
    pool, service, factory = database
    await service.record(change())
    results = await asyncio.gather(
        *(service.claim_link(user, -10, f"req-{user}", 60) for user in range(1, 1001)),
        return_exceptions=True,
    )
    assert results.count(0) == 5
    assert (
        sum(
            isinstance(result, DownloadError) and result.code == "group_rate_limited"
            for result in results
        )
        == 995
    )
    assert await pool.fetchval("SELECT count(*) FROM bot_users") == 5
    assert await pool.fetchval("SELECT count(*) FROM bot_conversations") == 5
    assert await pool.fetchval("SELECT cardinality(accepted_at) FROM group_request_limits") == 5
    restarted = GroupService(GroupRepository(pool, 123), factory)
    with pytest.raises(DownloadError) as error:
        await restarted.claim_link(2001, -10, "restart", 60)
    assert error.value.code == "group_rate_limited"
    assert await pool.fetchval("SELECT count(*) FROM request_limits WHERE user_id=2001") == 0
    await pool.execute(
        "UPDATE group_request_limits SET accepted_at=ARRAY[clock_timestamp()-interval '61 seconds']"
    )
    assert await restarted.claim_link(2001, -10, "expired", 60) == 0
    assert await pool.fetchval("SELECT cardinality(accepted_at) FROM group_request_limits") == 1


async def test_user_and_group_denials_consume_neither_other_quota(database):
    pool, service, _ = database
    await service.record(change())
    await service.record(change(chat=-20))
    assert await service.claim_link(1, -10, "one", 60) == 0
    assert await service.claim_link(1, -20, "user-denied", 60) > 0
    assert (
        await pool.fetchval(
            "SELECT cardinality(accepted_at) FROM group_request_limits WHERE chat_id=-20"
        )
        == 0
    )
    for user in range(2, 6):
        assert await service.claim_link(user, -10, str(user), 60) == 0
    with pytest.raises(DownloadError):
        await service.claim_link(6, -10, "group-denied", 60)
    assert await service.claim_link(6, -20, "other-group", 60) == 0
    assert await WorkflowRepository(pool, 123).claim_link(6, 6, "private", 60) > 0


async def test_admission_failure_rolls_back_both_quotas_and_state(database):
    pool, service, _ = database
    await service.record(change())
    with pytest.raises(asyncpg.NotNullViolationError):
        await service.claim_link(None, -10, "invalid", 60)
    assert await pool.fetchval("SELECT count(*) FROM group_request_limits") == 0
    assert await pool.fetchval("SELECT count(*) FROM request_limits") == 0
    assert await pool.fetchval("SELECT count(*) FROM bot_users") == 0


async def test_removed_group_cannot_admit_and_migration_preserves_quota(database):
    pool, service, _ = database
    await service.record(change(actor_id=70, title="Old title"))
    for user in range(1, 6):
        await service.claim_link(user, -10, str(user), 60)
    when = NOW + timedelta(seconds=10)
    await service.migrate(-10, -1000000000020, when)
    await service.migrate(-10, -1000000000020, when)
    old, new = await service.get(-10), await service.get(-1000000000020)
    assert not old.is_active and old.migrated_to == new.chat_id
    assert new.is_active and new.chat_kind == "supergroup"
    assert new.title == "Old title" and new.added_by_user_id == 70
    assert (
        await pool.fetchval(
            "SELECT cardinality(accepted_at) FROM group_request_limits WHERE chat_id=$1",
            new.chat_id,
        )
        == 5
    )
    assert await pool.fetchval("SELECT count(*) FROM group_events WHERE kind='migrated'") == 2
    for chat, reason in ((-10, "group_unavailable"), (new.chat_id, "group_rate_limited")):
        with pytest.raises(DownloadError) as error:
            await service.claim_link(20, chat, "after-migration", 60)
        assert error.value.code == reason


async def test_limiter_group_denial_does_not_install_a_user_denial_lease(database):
    pool, service, _ = database
    await service.record(change())
    await service.record(change(chat=-20))
    limiter = RequestLimiter(60, admission_repository=WorkflowRepository(pool, 123), groups=service)
    for user in range(1, 6):
        assert await limiter.check_link(user, -10, str(user))
    with pytest.raises(DownloadError):
        await limiter.check_link(6, -10, "full")
    assert (6, "inspect") not in limiter._deadlines
    assert await limiter.check_link(6, -20, "available")


async def test_other_bot_groups_and_quota_are_isolated(database):
    pool, service, _ = database
    await service.record(change())
    other = GroupService(GroupRepository(pool, 999), lambda: PostgresUnitOfWork(pool, 999))
    assert await other.get(-10) is None
    await other.record(change())
    assert await service.claim_link(1, -10, "first-bot", 60) == 0
    assert await other.claim_link(1, -10, "second-bot", 60) == 0


async def test_permission_recovery_logs_real_transition_and_old_migrated_chat_stays_inactive(
    database,
):
    pool, service, _ = database
    await service.record(change())
    await service.record(replace(change(sequence=2, admin=True), kind="standing_refreshed"))
    assert await pool.fetchval("SELECT count(*) FROM group_events WHERE kind='promoted'") == 1
    await service.migrate(-10, -1000000000020, NOW + timedelta(seconds=4))
    await service.record(change(sequence=5))
    row = await service.get(-10)
    assert row.is_active is False and row.migrated_to == -1000000000020


async def test_group_event_audit_and_snapshot_roll_back_together(database):
    pool, _, factory = database
    with pytest.raises(RuntimeError):
        async with factory() as uow:
            await uow.groups.record(change())
            raise RuntimeError("Rollback test")
    assert await pool.fetchval("SELECT count(*) FROM bot_groups") == 0
    assert await pool.fetchval("SELECT count(*) FROM group_events") == 0


async def test_ordinary_activity_does_not_advance_membership_watermark(database):
    _, service, _ = database
    await service.record(change())
    await service.record(
        replace(
            change(sequence=10),
            kind="observed",
            stream="observation",
            sequence=0,
            priority=0,
            event_key="message:100",
        )
    )
    await service.record(change("promoted", sequence=5, admin=True))
    row = await service.get(-10)
    assert row.is_admin is True
    assert row.last_seen_at >= NOW + timedelta(seconds=10)


async def test_older_dated_join_cannot_reverse_a_later_definite_permission_probe(database):
    _, service, _ = database
    await service.record(change())
    await service.record(
        replace(
            change(sequence=10, active=False),
            kind="standing_refreshed",
            stream="probe",
            sequence=0,
            priority=0,
        )
    )
    await service.record(change(sequence=5))
    assert (await service.get(-10)).is_active is False


async def test_upgrade_repairs_legacy_first_adder_and_admin_date_without_erasing_audit(database):
    pool, service, _ = database
    await service.record(change(actor_id=50))
    await service.record(change("removed", sequence=2, active=False))
    await service.record(change("added", sequence=3, actor_id=51))
    # Recreate the two legacy defects and the pre-upgrade version ledger.
    await pool.execute("ALTER TABLE bot_groups DROP CONSTRAINT bot_groups_admin_since_standing")
    await pool.execute("DELETE FROM app_schema_migrations WHERE version=9")
    await pool.execute("UPDATE bot_groups SET added_by_user_id=51,admin_since=now()")
    await asyncio.gather(migrate(pool), migrate(pool))
    row = await service.get(-10)
    assert row.added_by_user_id == 50 and row.admin_since is None
    assert row.readded_at and row.removed_at
    assert await pool.fetchval("SELECT count(*) FROM group_events") == 3
    assert await pool.fetchval("SELECT count(*) FROM app_schema_migrations WHERE version=9") == 1
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("UPDATE bot_groups SET admin_since=now()")


async def test_migration_retires_old_admin_date_and_returns_immutable_domain_snapshots(database):
    from dataclasses import FrozenInstanceError

    from schemas.group import GroupSnapshot

    _, service, _ = database
    await service.record(change("added", admin=True, actor_id=50))
    assert (await service.get(-10)).admin_since is not None
    await service.migrate(-10, -1000000000020, NOW + timedelta(seconds=5))
    old = await service.get(-10)
    assert isinstance(old, GroupSnapshot) and old.admin_since is None and old.is_admin is False
    with pytest.raises(FrozenInstanceError):
        old.is_active = True


async def test_upgrade_keeps_original_attribution_across_migration_and_later_readd(database):
    pool, service, _ = database
    target = -1000000000020
    await service.record(change(actor_id=50))
    await service.migrate(-10, target, NOW + timedelta(seconds=2))
    await service.record(
        replace(change("removed", chat=target, sequence=3, active=False), chat_kind="supergroup")
    )
    await service.record(
        replace(change("added", chat=target, sequence=4, actor_id=51), chat_kind="supergroup")
    )
    await pool.execute("DELETE FROM app_schema_migrations WHERE version=9")
    await pool.execute("UPDATE bot_groups SET added_by_user_id=51 WHERE chat_id=$1", target)
    await migrate(pool)
    assert (await service.get(target)).added_by_user_id == 50
    assert (await service.get(-10)).added_by_user_id == 50
