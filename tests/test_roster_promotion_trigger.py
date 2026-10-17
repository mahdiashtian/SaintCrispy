"""Promotion creates runs; idle startup neither seeds work nor replays reports."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_admin_reference_database import app as app

from bot.roster_crawl import RosterCrawler
from bot.roster_progress import RosterProgress, progress_text
from repositories.postgres.roster_crawl import RosterCrawlRepository
from schemas.group import GroupChange
from schemas.roster_crawl import RosterUser
from services.roster_crawl import RosterCrawlService


def service(app):
    return RosterCrawlService(RosterCrawlRepository(app.pool, app.bot), app.factory)


async def test_polling_existing_admin_without_crawl_does_not_create_startup_work(app):
    await app.groups.record(
        GroupChange(-10, "promoted", "old-promotion", app.now, "group", admin=True)
    )
    roster = service(app)
    for _ in range(3):
        assert await roster.claim() is None
    assert await roster.progress(-10) is None


@pytest.mark.parametrize("status", ["running", "done", "paused", "failed"])
async def test_every_repromotion_resets_run_and_fences_old_workers(app, status):
    app.groups.roster_enabled = True
    roster = service(app)
    await app.groups.record(GroupChange(-10, "promoted", "first", app.now, "group", admin=True))
    first = await roster.claim()
    first = await roster.page(
        first, [RosterUser(7)], [8], fence=await roster.fence(), read_at=app.now, offset=1, total=4
    )
    if status in {"done", "failed"}:
        await roster.finish(first, "converged", failed=status == "failed")
    elif status == "paused":
        await roster.pause(first, "flood_wait", 60, flood=True)
    await app.groups.record(
        GroupChange(-10, "demoted", "demote", app.now + timedelta(seconds=1), "group", admin=False)
    )
    await app.groups.record(
        GroupChange(-10, "promoted", "again", app.now + timedelta(seconds=2), "group", admin=True)
    )
    second = await roster.claim()
    assert second and second.run_id != first.run_id
    assert (
        second.offset_count
        == second.seen_unique
        == second.stored_new
        == second.skipped_accounts
        == 0
    )
    assert second.pass_number == 1
    assert not await roster.finish(first, "stale")
    assert await app.pool.fetchval("SELECT count(*) FROM roster_crawl_seen") == 0
    await app.groups.record(
        GroupChange(
            -10, "promoted", "duplicate-state", app.now + timedelta(seconds=3), "group", admin=True
        )
    )
    assert (await roster.progress(-10)).run_id == second.run_id


async def test_finished_run_is_not_claimed_again_after_service_restart(app):
    app.groups.roster_enabled = True
    await app.groups.record(GroupChange(-10, "promoted", "first", app.now, "group", admin=True))
    roster = service(app)
    first = await roster.claim()
    await roster.finish(first, "converged")
    assert await service(app).claim() is None
    assert (await roster.progress(-10)).run_id == first.run_id


async def test_interrupted_promotion_work_resumes_same_run_after_restart(app):
    app.groups.roster_enabled = True
    await app.groups.record(GroupChange(-10, "promoted", "first", app.now, "group", admin=True))
    first = await service(app).claim()
    await app.pool.execute(
        "UPDATE group_roster_crawls SET locked_until=clock_timestamp()-interval '1 second'"
    )
    second = await service(app).claim()
    assert second.run_id == first.run_id and second.locked_by != first.locked_by


@pytest.mark.parametrize("status", ["missing", "running", "done"])
async def test_migration_carries_only_pending_promotion_work(app, status):
    app.groups.roster_enabled = status != "missing"
    await app.groups.record(GroupChange(-10, "promoted", "first", app.now, "group", admin=True))
    roster = service(app)
    first = await roster.claim()
    if status == "done":
        await roster.finish(first, "converged")
    app.groups.roster_enabled = True
    target = -1000000000077
    await app.groups.migrate(-10, target, app.now + timedelta(seconds=1))
    assert ((await roster.progress(target)) is not None) == (status == "running")


@pytest.mark.parametrize("saved", [{}, {"owner": 7, "message_id": 22}])
async def test_idle_startup_does_not_replay_historical_completed_report(saved):
    roster = SimpleNamespace(summary=AsyncMock(), report_state=AsyncMock(return_value=saved))
    client = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
    report = RosterProgress(client, roster, 7, interval=0)
    await report.update()
    await report.update()
    roster.summary.assert_not_awaited()
    client.send_message.assert_not_awaited()
    client.edit_message.assert_not_awaited()


async def test_idle_worker_does_not_activate_reporting(monkeypatch):
    roster = SimpleNamespace(
        claim=AsyncMock(side_effect=[None, asyncio.CancelledError()]), summary=AsyncMock()
    )
    report = RosterProgress(None, roster, 7)
    worker = RosterCrawler(None, roster, None, None, 123, progress=report)
    monkeypatch.setattr("bot.roster_crawl.asyncio.sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await worker._loop()
    roster.summary.assert_not_awaited()


async def test_claimed_work_after_restart_reuses_existing_report_anchor():
    roster = SimpleNamespace(
        summary=AsyncMock(
            return_value=dict(
                total=1,
                pending=1,
                failed=0,
                done=0,
                seen_unique=0,
                stored_new=0,
                skipped_accounts=0,
            )
        ),
        recent=AsyncMock(return_value=[]),
        report_state=AsyncMock(return_value={"owner": 7, "message_id": 22}),
    )
    client = SimpleNamespace(
        send_message=AsyncMock(), edit_message=AsyncMock(return_value=SimpleNamespace(id=22))
    )
    await RosterProgress(client, roster, 7).update(active=True)
    client.send_message.assert_not_awaited()
    assert client.edit_message.call_args.args[:2] == (7, 22)


def test_recent_groups_always_include_escaped_name_and_exact_numeric_id():
    summary = dict(pending=0, failed=0, done=1, seen_unique=30, stored_new=29, skipped_accounts=3)
    text = progress_text(
        summary,
        [
            dict(
                title="Example <Group>",
                chat_id=-1000000000077,
                claimed_total=34,
                stopped_because="converged",
                seen_unique=30,
                skipped_accounts=3,
                status="done",
            )
        ],
    )
    assert "Example &lt;Group&gt;" in text
    assert "<code>-1000000000077</code>" in text
