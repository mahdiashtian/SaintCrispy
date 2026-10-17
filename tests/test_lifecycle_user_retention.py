"""Couplyo lifecycle parity: role changes never remove stored human records."""

from datetime import timedelta

import pytest
from test_admin_reference_database import app as app

from repositories.postgres.roster_crawl import RosterCrawlRepository
from schemas.group import GroupChange
from schemas.membership import MembershipEvent
from schemas.roster_crawl import RosterUser
from services.roster_crawl import RosterCrawlService


async def snapshot(app):
    return {
        table: [
            dict(row) for row in await app.pool.fetch(f"SELECT * FROM {table} ORDER BY {order}")
        ]
        for table, order in (
            ("bot_users", "user_id"),
            ("group_memberships", "chat_id,user_id"),
            ("membership_events", "chat_id,event_key"),
            ("user_media_history", "user_id,content_id"),
        )
    }


async def seed(app):
    app.groups.roster_enabled = True
    await app.groups.record(GroupChange(-10, "promoted", "first", app.now, "group", admin=True))
    await app.groups.record(
        GroupChange(-11, "added", "other", app.now, "group", active=True, admin=False)
    )
    roster = RosterCrawlService(RosterCrawlRepository(app.pool, app.bot), app.factory)
    crawl = await roster.claim()
    await roster.page(
        crawl,
        [RosterUser(7, first_name="Human"), RosterUser(8, first_name="Other")],
        [],
        fence=await roster.fence(),
        read_at=app.now,
        offset=2,
        total=2,
    )
    for chat, identity in ((-10, 7), (-10, 8), (-11, 7)):
        await app.membership.persist(
            MembershipEvent(
                chat, identity, f"message:{identity}", "message", app.now + timedelta(seconds=1)
            )
        )
    await app.workflow.touch_user(7, started=True)
    await app.admin.mutate(999, "grant", 7)
    await app.pool.execute(
        """INSERT INTO user_media_history
        (telegram_account_id,user_id,site,content_id,quality,last_method)
        SELECT $1,id,'youtube','reference','720p','reused' FROM unnest(ARRAY[7,8]) id""",
        app.bot,
    )
    return roster, crawl


@pytest.mark.parametrize("kind", ["demoted", "removed"])
async def test_bot_demotion_or_removal_preserves_all_users_memberships_and_history(app, kind):
    roster, crawl = await seed(app)
    before = await snapshot(app)
    await app.groups.record(
        GroupChange(
            -10,
            kind,
            "loss",
            app.now + timedelta(seconds=2),
            "group",
            active=False if kind == "removed" else None,
            admin=False,
        )
    )
    assert await snapshot(app) == before
    group = await app.groups.get(-10)
    assert group.is_admin is False
    assert group.is_active is (kind != "removed")
    assert (await app.groups.get(-11)).is_active is True
    assert (await roster.progress(-10)).status == ("cancelled" if kind == "removed" else "paused")
    assert not await roster.valid(crawl)


async def test_repromotion_clears_only_run_tracking_and_keeps_permanent_human_data(app):
    roster, old = await seed(app)
    before = await snapshot(app)
    assert await app.pool.fetchval("SELECT count(*) FROM roster_crawl_seen") == 2
    await app.groups.record(
        GroupChange(-10, "demoted", "demote", app.now + timedelta(seconds=2), "group", admin=False)
    )
    await app.groups.record(
        GroupChange(-10, "promoted", "again", app.now + timedelta(seconds=3), "group", admin=True)
    )
    assert await snapshot(app) == before
    assert await app.pool.fetchval("SELECT count(*) FROM roster_crawl_seen") == 0
    assert (await roster.progress(-10)).run_id != old.run_id


@pytest.mark.parametrize("identity", [7, 8])
async def test_member_departure_deactivates_only_that_membership_and_keeps_global_user(
    app, identity
):
    await seed(app)
    before = await snapshot(app)
    await app.membership.persist(
        MembershipEvent(
            -10,
            identity,
            "left",
            "left",
            app.now + timedelta(seconds=2),
            sequence=2,
            stream="basic",
        )
    )
    after = await snapshot(app)
    assert [row["user_id"] for row in after["bot_users"]] == [7, 8]
    assert after["user_media_history"] == before["user_media_history"]
    assert len(after["group_memberships"]) == len(before["group_memberships"])
    member = next(
        row
        for row in after["group_memberships"]
        if row["chat_id"] == -10 and row["user_id"] == identity
    )
    assert member["is_active"] is False
    for row in before["group_memberships"]:
        if (row["chat_id"], row["user_id"]) != (-10, identity):
            assert row in after["group_memberships"]
    admin = next(row for row in after["bot_users"] if row["user_id"] == 7)
    assert admin["is_admin"] and admin["is_started"]
