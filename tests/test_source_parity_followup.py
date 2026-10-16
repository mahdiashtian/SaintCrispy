"""Real SQL contracts for source data, calendar activity and lifecycle copy."""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import asyncpg
import pytest
from telethon import types
from test_admin_reference_database import app as app

from bot.operations import EffectDelivery
from bot.texts.group_lifecycle import admin_event
from core.jalali import to_shamsi
from db.postgres.engine import migrate
from db.postgres.migrations import MIGRATIONS
from db.postgres.schema import LEDGER_SCHEMA
from repositories.postgres.daily_stat import DailyStatsRepository
from repositories.postgres.group import GroupRepository
from repositories.postgres.outbox import OutboxRepository
from schemas.admin import PendingEffect
from schemas.group import GroupChange
from schemas.membership import MembershipEvent
from services.group import GroupService
from services.management import TEHRAN, ManagementService
from services.user import UserService
from tools.diagnose_group_telegram import probe

CHANNEL = {"id": -(10**12 + 77), "title": "Archive", "link": "https://t.me/test_archive"}


async def test_normalized_channel_flags_and_watch_rows_have_one_authoritative_copy(app):
    for key, value in (
        ("forced_channels", [CHANNEL]),
        ("archive", CHANNEL),
        ("storage", CHANNEL),
        ("watches", [{"chat_id": -10, "channel": CHANNEL, "kinds": ["text"]}]),
    ):
        await app.admin.channels.update(999, key, value)
    row = await app.pool.fetchrow("SELECT * FROM channels")
    assert row["is_mandatory"] and row["is_archive"] and row["is_storage"]
    assert await app.pool.fetchval("SELECT count(*) FROM channels") == 1
    watch = await app.pool.fetchrow("SELECT * FROM group_watches")
    assert watch["chat_id"] == -10 and json.loads(watch["kinds"]) == ["text"]
    value = json.loads(
        await app.pool.fetchval("SELECT value FROM app_settings WHERE key='channel_management'")
    )
    assert not set(value) & {"forced_channels", "archive", "storage", "watches"}
    assert (await app.admin.channels.get())["archive"] == CHANNEL


async def test_unrelated_configuration_edit_does_not_rewrite_a_thousand_watch_rows(app):
    watches = [{"chat_id": -n, "channel": CHANNEL, "kinds": ["text"]} for n in range(1, 1001)]
    # Exercise the storage repository's bulk shape independently of panel list caps.
    async with app.factory() as uow:
        await uow.admin.set_setting("channel_management", {"watches": watches})
    before = await app.pool.fetch("SELECT id,updated_at FROM group_watches ORDER BY id")
    await app.admin.channels.update(999, "developer_id", 7)
    after = await app.pool.fetch("SELECT id,updated_at FROM group_watches ORDER BY id")
    assert before == after and len(after) == 1000


async def test_first_start_attribution_is_immutable_and_group_activity_is_separate(app):
    service = UserService(app.workflow, app.factory)
    await service.start(7, 7, source="unknown", source_chat_id=-10)
    first = await app.pool.fetchrow("SELECT * FROM bot_users WHERE user_id=7")
    await service.start(7, 7, source="plain")
    await app.membership.persist(MembershipEvent(-10, 7, "message", "message", app.now))
    await app.membership.persist(
        MembershipEvent(-10, 7, "leave", "left", app.now + timedelta(seconds=10))
    )
    row = await app.pool.fetchrow("SELECT * FROM bot_users WHERE user_id=7")
    assert row["start_source"] == "unknown" and row["start_chat_id"] == -10
    assert row["started_at"] == first["started_at"] and row["last_active_at"] == app.now


async def test_buffered_message_keeps_ingest_admin_status_across_demotion(app):
    stats = DailyStatsRepository(app.pool, app.bot)
    await app.groups.record(GroupChange(-10, "promoted", "p", app.now, "group", admin=True))
    await app.membership.persist(
        MembershipEvent(-10, 7, "warm", "message", app.now, observed_admin=True)
    )
    queued = MembershipEvent(
        -10, 7, "buffered", "message", app.now + timedelta(seconds=1), observed_admin=True
    )
    await app.membership.observe(queued)
    await app.groups.record(
        GroupChange(-10, "demoted", "d", app.now + timedelta(seconds=2), "group", admin=False)
    )
    await app.membership.flush()
    assert (await stats.snapshot(app.now.astimezone(TEHRAN).date()))["active_senders"] == 1
    await app.membership.persist(
        MembershipEvent(-10, 8, "nonadmin", "message", app.now, observed_admin=False)
    )
    await app.groups.record(
        GroupChange(-10, "promoted", "p2", app.now + timedelta(seconds=3), "group", admin=True)
    )
    assert (await stats.snapshot(app.now.astimezone(TEHRAN).date()))["active_senders"] == 1


async def test_daily_history_is_sealed_once_and_never_fabricates_preinstrumentation_days(app):
    manager = ManagementService(app.admin, app.groups, app.bot)
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    await manager.rollover(now)
    assert await app.pool.fetchval("SELECT count(*) FROM daily_stats") == 0
    await manager.rollover(now + timedelta(days=1))
    before = await app.pool.fetchrow("SELECT * FROM daily_stats")
    await app.workflow.touch_user(7, started=True)
    manager._sealed_day = None
    await manager.rollover(now + timedelta(days=1))
    assert await app.pool.fetchrow("SELECT * FROM daily_stats") == before
    assert len(await app.admin.history(999)) == 1


@pytest.mark.parametrize(
    "scope,expected", [("both", {-10, -11}), ("admin", {-11}), ("non_admin", {-10})]
)
async def test_group_audience_and_active_member_exclusion_match_source(app, scope, expected):
    await app.groups.record(
        GroupChange(-11, "added", "seed11", app.now, "group", active=True, admin=True)
    )
    await app.membership.persist(MembershipEvent(-10, 7, "msg", "message", app.now))
    repo = OutboxRepository(app.pool, app.bot)
    job = {
        "kind": "broadcast",
        "cursor": None,
        "created_at": datetime.now(UTC),
        "payload": {"audience": "groups", "group_audience": scope},
    }
    assert set(await repo.targets(job)) == expected
    job["payload"]["exclude_members"] = [7]
    assert set(await repo.targets(job)) == expected - {-10}
    await app.membership.persist(
        MembershipEvent(-10, 7, "leave", "left", app.now + timedelta(seconds=1))
    )
    assert set(await repo.targets(job)) == expected


async def test_lifecycle_loss_notice_retains_last_admin_details_and_migration_is_deduped(app):
    groups = GroupService(
        GroupRepository(app.pool, app.bot), app.factory, main_admin_id=999, management_enabled=True
    )
    await groups.record(
        GroupChange(-10, "promoted", "promote", app.now + timedelta(seconds=1), "group", admin=True)
    )
    await groups.metadata(-10, 7, [8], 23, "https://t.me/test_group")
    await groups.record(
        GroupChange(
            -10,
            "removed",
            "removed",
            app.now + timedelta(seconds=2),
            "group",
            active=False,
            admin=False,
        )
    )
    rows = await app.pool.fetch("SELECT payload FROM outbox_events WHERE target_id=999 ORDER BY id")
    notice = json.loads(rows[-1]["payload"])["group_event"]
    assert notice["group"]["is_admin"] is True and notice["group"]["member_count"] == 23
    text = admin_event(notice)
    assert "حذف/اخراج" in text and "آخرین چیزی" in text and "ادمین بودم: بله" in text
    new_id = -(10**12 + 77)
    await asyncio.gather(
        *(groups.migrate(-10, new_id, app.now + timedelta(seconds=3)) for _ in range(4))
    )
    assert (
        await app.pool.fetchval(
            "SELECT count(*) FROM outbox_events WHERE dedupe_key LIKE 'group-migration:%'"
        )
        == 1
    )


async def test_queued_add_announcement_is_suppressed_after_promotion(app):
    manager = ManagementService(app.admin, app.groups, app.bot)
    effect = PendingEffect(
        1,
        "message",
        -10,
        {"group_announcement": "added", "management": {"feature": "group_lifecycle"}},
        123,
        None,
        1,
    )
    assert await manager.allowed(effect)
    await app.groups.record(GroupChange(-10, "promoted", "p", app.now, "group", admin=True))
    assert not await manager.allowed(effect)


async def test_manual_report_quota_is_atomic_and_separate_by_period(app):
    manager = ManagementService(app.admin, app.groups, app.bot)
    accepted = await asyncio.gather(*(manager.request_report(7, -10, "day", 1) for _ in range(50)))
    assert sum(accepted) == 1
    assert await manager.request_report(7, -10, "week", 1)
    assert await manager.request_report(999, -10, "day", 1)


def test_source_jalali_conversion_keeps_reference_format():
    assert to_shamsi(datetime(2026, 10, 16, 12, tzinfo=UTC)) == "۱۴۰۵/۰۷/۲۴ ۱۵:۳۰"


async def test_live_probe_is_read_only_and_never_attempts_interactive_user_login():
    client = AsyncMock()
    client.get_me.return_value = types.User(id=12345, bot=True)
    calls = []

    def factory(*args, **kwargs):
        calls.append(kwargs)
        return client

    result = await probe(
        {"API_ID": "1", "API_HASH": "hash", "BOT_TOKEN": "12345:token"}, factory=factory
    )
    assert result["authenticated"] and not result["group_checked"]
    assert calls[0]["receive_updates"] is False and calls[0]["catch_up"] is False
    client.send_code_request.assert_not_awaited()
    client.send_message.assert_not_awaited()


async def test_revision_eleven_upgrade_preserves_existing_destinations_and_activity(app):
    schema = "source_upgrade_" + uuid.uuid4().hex
    await app.pool.execute(f'CREATE SCHEMA "{schema}"')
    try:
        async with asyncpg.create_pool(
            os.environ["TEST_DATABASE_URL"],
            min_size=1,
            max_size=3,
            server_settings={"search_path": schema},
        ) as pool:
            async with pool.acquire() as c:
                await c.execute(LEDGER_SCHEMA)
                for version, name, sql in MIGRATIONS[:11]:
                    await c.execute(sql)
                    await c.execute(
                        "INSERT INTO app_schema_migrations(version,name) VALUES($1,$2)",
                        version,
                        name,
                    )
                config = {
                    "archive": CHANNEL,
                    "storage": CHANNEL,
                    "forced_channels": [CHANNEL],
                    "watches": [{"chat_id": -10, "channel": CHANNEL, "kinds": ["video"]}],
                    "developer_id": 7,
                }
                await c.execute(
                    "INSERT INTO app_settings(telegram_account_id,key,value) VALUES($1,'channel_management',$2::jsonb)",
                    app.bot,
                    json.dumps(config),
                )
            await asyncio.gather(*(migrate(pool) for _ in range(3)))
            row = await pool.fetchrow("SELECT * FROM channels")
            assert row["is_archive"] and row["is_mandatory"] and row["is_storage"]
            assert await pool.fetchval("SELECT count(*) FROM group_watches") == 1
            assert json.loads(await pool.fetchval("SELECT value FROM app_settings")) == {
                "developer_id": 7
            }
    finally:
        await app.pool.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def test_unknown_legacy_activity_eligibility_stays_unknown_after_replay(app):
    await app.groups.record(GroupChange(-10, "promoted", "p", app.now, "group", admin=True))
    await app.stream.append(MembershipEvent(-10, 7, "legacy", "message", app.now))
    await app.membership.flush()
    assert await app.pool.fetchval("SELECT observed_admin FROM membership_events") is None


async def test_unknown_channel_titles_do_not_disable_configured_routes(app):
    await app.pool.execute(
        "INSERT INTO channels(telegram_account_id,channel_telegram_id,channel_link,is_mandatory) VALUES($1,$2,$3,TRUE)",
        app.bot,
        CHANNEL["id"],
        CHANNEL["link"],
    )
    config = await app.admin.channels.get()
    assert len(config["forced_channels"]) == 1 and config["forced_channels"][0]["title"] == ""


@pytest.mark.parametrize("active", [True, False])
async def test_bulk_activity_matches_single_event_counters_and_leave_barriers(app, active):
    from repositories.postgres.unit_of_work import PostgresUnitOfWork
    from services.membership import MembershipService

    other_bot = app.bot + 1
    other_groups = GroupService(
        GroupRepository(app.pool, other_bot), lambda: PostgresUnitOfWork(app.pool, other_bot)
    )
    await other_groups.record(
        GroupChange(-10, "added", "seed", app.now, "group", active=active, admin=False)
    )
    await app.groups.record(
        GroupChange(-10, "standing_refreshed", "active", app.now, "group", active=active)
    )
    other = MembershipService(None, lambda: PostgresUnitOfWork(app.pool, other_bot))
    left = MembershipEvent(-10, 7, "left", "left", app.now, 2, stream="participant")
    for service in (app.membership, other):
        await service.profile(7, None, "Human", None)
        await service.persist(left)
    events = [
        MembershipEvent(
            -10,
            7,
            f"msg:{i}",
            "message",
            app.now + timedelta(days=shift),
            observed_admin=i % 2 == 0,
        )
        for i, shift in enumerate((-20, -6, -1, 0, 1, -4, 3, -20))
    ]
    for event in events + events:
        await other.persist(event)
    async with app.factory() as uow:
        assert await uow.memberships.apply_messages(events + events) == len(events)
    fields = (
        "is_active,state_changed_at,last_seen_at,message_count,msg_week,msg_this_week,msg_last_week"
    )
    rows = await app.pool.fetch(
        f"SELECT {fields} FROM group_memberships WHERE chat_id=-10 ORDER BY telegram_account_id"
    )
    assert len(rows) == 2 and dict(rows[0]) == dict(rows[1])


async def test_protected_forward_blocks_following_same_route_but_flood_wait_does_not(app):
    from telethon import errors

    from schemas.admin import PermanentEffect, RetryEffect

    manager = ManagementService(app.admin, app.groups, app.bot)
    await app.admin.channels.update(999, "storage", CHANNEL)
    await app.admin.channels.update(999, "storage_kinds", ["text"])
    client = AsyncMock(side_effect=errors.ChatForwardsRestrictedError(None))
    client.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(77, 123))
    tracker = type("Tracker", (), {"send_failure": AsyncMock()})()
    payload = {
        "source_peer": -10,
        "source_id": 1,
        "policy": "management",
        "management": {"feature": "storage", "source_peer": -10, "kind": "text"},
    }
    effect = PendingEffect(1, "forward", CHANNEL["id"], payload, 123, None, 1)
    with pytest.raises(PermanentEffect):
        await EffectDelivery(client, tracker, app.groups, management=manager).deliver(effect)
    assert await manager.enqueue_storage(-10, 2, "text", 7) == 0
    app.admin.channels.invalidate()
    client.side_effect = errors.FloodWaitError(None, 3)
    with pytest.raises(RetryEffect):
        await EffectDelivery(client, tracker, app.groups, management=manager).deliver(effect)
    assert not manager.route_refused(-10, CHANNEL["id"])


async def test_report_window_excludes_later_events_and_counts_departed_speakers(app):
    manager = ManagementService(app.admin, app.groups, app.bot)
    await app.membership.persist(MembershipEvent(-10, 7, "one", "message", app.now))
    await app.membership.persist(
        MembershipEvent(-10, 7, "left", "left", app.now + timedelta(seconds=1))
    )
    await app.membership.persist(
        MembershipEvent(-10, 8, "later", "message", app.now + timedelta(days=1))
    )
    _, caption = await manager.report(
        -10,
        period="day",
        start=(app.now - timedelta(seconds=1)).isoformat(),
        end=(app.now + timedelta(seconds=2)).isoformat(),
    )
    assert "کاربران پیام‌دهنده: 1" in caption and "پیام‌های امروز: 1" in caption


async def test_purged_account_cannot_return_from_queued_messages_profile_csv_or_admission(app):
    from schemas.media import DownloadError

    await app.membership.persist(MembershipEvent(-10, 7, "original", "message", app.now))
    await app.stream.append(MembershipEvent(-10, 7, "queued", "message", app.now))
    assert await app.membership.purge_invalid(7, deleted=True, root_id=999)
    await app.membership.flush()
    await app.membership.profile(7, None, "Stale profile", None)
    async with app.factory() as uow:
        await uow.admin.import_users([(7, None, "CSV", None)])
        await uow.workflow.touch_user(7, started=True)
        assert await uow.workflow.claim_link(7, 7, "old-request", 60) == 60
    with pytest.raises(DownloadError):
        await app.admin.mutate(999, "grant", 7)
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users WHERE user_id=7") == 0
    assert await app.pool.fetchval("SELECT count(*) FROM group_memberships WHERE user_id=7") == 0
    assert (
        await app.pool.fetchval("SELECT count(*) FROM user_account_tombstones WHERE user_id=7") == 1
    )


async def test_old_roster_and_public_backup_cannot_resurrect_purged_accounts(app):
    from test_couplyo_roster import page, setup

    from schemas.roster_crawl import RosterUser

    await app.membership.persist(MembershipEvent(-10, 7, "old", "message", app.now))
    backup = (await app.admin.maintenance.backup(999)).data
    service = await setup(app)
    crawl = await service.claim()
    assert await app.membership.purge_invalid(7, bot=True, root_id=999)
    result = await page(service, crawl, [RosterUser(7)])
    assert result.seen_unique == 0 and result.skipped_accounts == 1
    token, _ = await app.admin.maintenance.prepare_restore(999, backup)
    await app.admin.maintenance.confirm_restore(999, token)
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users WHERE user_id=7") == 0
    assert await app.pool.fetchval("SELECT count(*) FROM group_memberships WHERE user_id=7") == 0


async def test_purge_never_fences_root_admin_or_unverified_accounts(app):
    for identity in (7, 8, 999):
        await app.membership.profile(identity, None, "Human", None)
    await app.admin.mutate(999, "grant", 7)
    assert not await app.membership.purge_invalid(7, bot=True, root_id=999)
    assert not await app.membership.purge_invalid(8, root_id=999)
    assert not await app.membership.purge_invalid(999, deleted=True, root_id=999)
    assert await app.pool.fetchval("SELECT count(*) FROM user_account_tombstones") == 0


async def test_batch_activity_and_purge_have_consistent_lock_order_and_no_resurrection(app):
    for identity in range(1, 21):
        await app.membership.persist(
            MembershipEvent(-10, identity, f"seed:{identity}", "message", app.now)
        )

    async def batch():
        async with app.factory() as uow:
            await uow.memberships.apply_messages(
                [
                    MembershipEvent(-10, identity, f"race:{identity}", "message", app.now)
                    for identity in range(1, 21)
                ]
            )

    await asyncio.wait_for(
        asyncio.gather(
            batch(),
            *(
                app.membership.purge_invalid(identity, deleted=True, root_id=999)
                for identity in range(1, 21)
            ),
        ),
        timeout=10,
    )
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 0
    assert await app.pool.fetchval("SELECT count(*) FROM user_account_tombstones") == 20
