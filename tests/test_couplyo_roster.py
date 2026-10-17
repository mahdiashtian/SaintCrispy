"""Real SQL roster checkpoints, lifecycle ordering and Telegram-shaped paging."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types
from test_admin_reference_database import app as app

from bot.roster_crawl import RosterCrawler
from repositories.postgres.group import GroupRepository
from repositories.postgres.roster_crawl import RosterCrawlRepository
from schemas.group import GroupChange, GroupStanding, Standing
from schemas.membership import MembershipEvent
from schemas.roster_crawl import RosterUser
from services.group import GroupService
from services.roster_crawl import RosterCrawlService

CHAT = -(10**12 + 10)


async def setup(app, chat=CHAT):
    await app.groups.record(
        GroupChange(
            chat,
            "added",
            f"seed:{chat}",
            app.now,
            "supergroup" if chat == CHAT else "group",
            active=True,
            admin=True,
            can_send=True,
        )
    )
    service = RosterCrawlService(RosterCrawlRepository(app.pool, app.bot), app.factory)
    await service.enqueue(chat)
    return service


async def page(service, crawl, users=(), skipped=(), **values):
    defaults = dict(fence=await service.fence(), read_at=datetime.now(UTC), offset=1, total=None)
    return await service.page(crawl, list(users), list(skipped), **(defaults | values))


async def test_one_thousand_enqueue_and_claim_contention_has_one_owned_run(app):
    service = await setup(app)
    assert not any(await asyncio.gather(*(service.enqueue(CHAT) for _ in range(1000))))
    claims = await asyncio.gather(*(service.claim() for _ in range(20)))
    assert sum(claim is not None for claim in claims) == 1
    first = next(claim for claim in claims if claim)
    await app.pool.execute("UPDATE group_roster_crawls SET locked_until=now()-interval '1 second'")
    second = await service.claim()
    assert second.locked_by != first.locked_by and second.run_id == first.run_id
    assert await page(service, first, [RosterUser(7)]) is None
    assert not await service.finish(first, "stale")
    assert (await page(service, second, [RosterUser(7)])).seen_unique == 1


async def test_bulk_page_dedupes_profiles_preserves_roles_and_does_not_fabricate_activity(app):
    service = await setup(app)
    await app.workflow.touch_user(7, started=True)
    await app.admin.mutate(999, "grant", 7)
    crawl = await service.claim()
    users = [RosterUser(identity, first_name="Human") for identity in range(7, 507)]
    result = await page(service, crawl, users + users, skipped=[7, 999, 999])
    assert result.seen_unique == 500 and result.stored_new == 500 and result.skipped_accounts == 1
    assert await app.pool.fetchval("SELECT count(*) FROM group_memberships") == 500
    assert await app.pool.fetchval("SELECT sum(message_count) FROM group_memberships") == 0
    assert await app.pool.fetchval(
        "SELECT bool_and(last_seen_at<now()-interval '29 days') FROM group_memberships"
    )
    assert (await app.admin.repository.user(7)).is_admin and (
        await app.admin.repository.user(7)
    ).is_started
    result = await page(service, result, users)
    assert result.seen_unique == 500 and result.stored_new == 500
    assert await app.pool.fetchval("SELECT count(*) FROM membership_events") == 0


async def test_page_failure_rolls_back_users_members_seen_and_cursor(app):
    service = await setup(app)
    crawl = await service.claim()
    with pytest.raises(Exception):
        await page(service, crawl, [RosterUser(7), RosterUser(8, first_name="bad\x00name")])
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 0
    assert await app.pool.fetchval("SELECT count(*) FROM roster_crawl_seen") == 0
    assert (await service.progress(CHAT)).offset_count == 0


@pytest.mark.parametrize("known", [False, True])
async def test_concurrent_departure_after_fence_prevents_roster_resurrection(app, known):
    service = await setup(app)
    if known:
        await app.membership.profile(7, None, "Known", None)
    crawl = await service.claim()
    fence = await service.fence()
    read_at = datetime.now(UTC)
    await app.membership.departure(CHAT, 7, read_at, "unique-left")
    if known:
        await app.membership.persist(
            MembershipEvent(CHAT, 7, "left", "left", read_at, 2, stream="participant")
        )
    else:
        assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 0
    result = await page(service, crawl, [RosterUser(7)], fence=fence, read_at=read_at)
    assert result.seen_unique == 1
    assert (
        await app.pool.fetchval("SELECT is_active FROM group_memberships WHERE user_id=7") is False
    )


async def test_old_departure_cannot_replace_new_roster_and_replay_does_not_move_fence(app):
    service = await setup(app)
    crawl = await service.claim()
    await page(service, crawl, [RosterUser(7)])
    old = datetime.now(UTC) - timedelta(days=1)
    await app.membership.persist(
        MembershipEvent(CHAT, 7, "old", "left", old, 2, stream="participant")
    )
    assert await app.pool.fetchval("SELECT is_active FROM group_memberships")
    await app.membership.departure(CHAT, 7, old, "same")
    first = await app.pool.fetchval("SELECT revision FROM membership_departures")
    await app.membership.departure(CHAT, 7, old, "same")
    assert await app.pool.fetchval("SELECT revision FROM membership_departures") == first


async def test_unknown_leave_never_creates_a_group_or_global_user(app):
    assert not await app.membership.persist(MembershipEvent(-777, 7, "left", "left", app.now))
    await app.membership.departure(-777, 7, app.now, "absent")
    assert await app.pool.fetchval("SELECT count(*) FROM bot_groups") == 1
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 0
    assert await app.pool.fetchval("SELECT count(*) FROM membership_departures") == 0


async def test_later_verified_human_promotes_skipped_id_and_prevents_false_convergence(app):
    service = await setup(app)
    current = await service.claim()
    current = await page(service, current, skipped=[7])
    assert current.seen_unique == 0 and current.skipped_accounts == 1
    current = await page(service, current, [RosterUser(7)])
    assert current.seen_unique == 1 and current.skipped_accounts == 0
    assert current.pass_new_count == 1 and current.stored_new == 1


async def test_roster_reactivation_fences_older_explicit_leave(app):
    service = await setup(app)
    old = datetime.now(UTC) - timedelta(seconds=20)
    await app.membership.profile(7, None, "Known", None)
    await app.membership.persist(
        MembershipEvent(CHAT, 7, "left1", "left", old, 1, stream="participant")
    )
    current = await service.claim()
    await page(service, current, [RosterUser(7)])
    await app.membership.persist(
        MembershipEvent(
            CHAT, 7, "left2", "left", old + timedelta(seconds=1), 2, stream="participant"
        )
    )
    assert await app.pool.fetchval("SELECT is_active FROM group_memberships") is True


async def test_demotion_releases_claim_and_promotion_starts_fresh_run(app):
    service = await setup(app)
    groups = GroupService(GroupRepository(app.pool, app.bot), app.factory, roster_enabled=True)
    crawl = await service.claim()
    await groups.record(
        GroupChange(
            CHAT, "demoted", "demote", app.now + timedelta(seconds=1), "supergroup", admin=False
        )
    )
    assert await page(service, crawl, [RosterUser(7)]) is None
    current = await service.progress(CHAT)
    assert current.status == "paused" and current.stopped_because == "not_admin"
    await groups.record(
        GroupChange(
            CHAT, "promoted", "promote", app.now + timedelta(seconds=2), "supergroup", admin=True
        )
    )
    current = await service.claim()
    assert current and current.run_id != crawl.run_id
    await service.pause(current, "flood_wait", 200, flood=True)
    assert not await service.enqueue(CHAT)
    assert await service.claim() is None


class Client:
    def __init__(self, pages=None, error=None, basic=None):
        self.pages = list(pages or [])
        self.error, self.basic = error, basic
        self.requests = []

    async def get_input_entity(self, value):
        return (
            types.InputPeerChat(10) if self.basic is not None else types.InputPeerChannel(10, 1234)
        )

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(
                chats=[
                    types.Channel(
                        10, "Group", types.ChatPhotoEmpty(), datetime.now(UTC), megagroup=True
                    )
                ],
                full_chat=SimpleNamespace(participants_count=3),
            )
        if isinstance(request, functions.messages.GetFullChatRequest):
            return self.basic
        if self.error:
            raise self.error
        return self.pages.pop(0)


def response(*users):
    return SimpleNamespace(
        participants=[SimpleNamespace(user_id=user.id) for user in users],
        users=list(users),
        count=3,
    )


def crawler(app, service, client, **values):
    tracker = SimpleNamespace(
        health=AsyncMock(return_value=GroupStanding(Standing.ADMIN, True)), send_failure=AsyncMock()
    )
    return RosterCrawler(client, service, app.groups, tracker, app.bot, delay=0, **values)


async def test_search_recent_multi_pass_crawl_saves_humans_and_explains_skips(app):
    service = await setup(app)
    humans = [types.User(7, first_name="One"), types.User(8, first_name="Two")]
    bot = types.User(9, bot=True)
    client = Client(
        [
            response(humans[0], bot),
            response(),
            response(humans[1], bot),
            response(),
            response(*humans, bot),
            response(),
            response(*humans, bot),
            response(),
        ]
    )
    await crawler(app, service, client).run(await service.claim())
    progress = await service.progress(CHAT)
    assert progress.status == "done" and progress.converged
    assert progress.seen_unique == 2 and progress.stored_new == 2 and progress.skipped_accounts == 1
    assert progress.claimed_total == 3 and not progress.is_complete and progress.accounted_for == 3
    pages = [r for r in client.requests if isinstance(r, functions.channels.GetParticipantsRequest)]
    assert all(request.hash == 0 and request.limit == 200 for request in pages)
    assert [request.offset for request in pages] == [0, 2, 0, 2, 0, 3, 0, 3]
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users WHERE account_kind='bot'") == 0


async def test_flood_wait_preserves_cursor_and_due_time(app):
    service = await setup(app)
    client = Client(error=errors.FloodWaitError(request=None, capture=200))
    worker = crawler(app, service, client)
    worker.rate = SimpleNamespace(reserve=AsyncMock(return_value=0), pause=AsyncMock())
    await worker.run(await service.claim())
    progress = await service.progress(CHAT)
    assert progress.status == "paused" and progress.flood_waits == 1 and progress.offset_count == 0
    assert progress.available_at > datetime.now(UTC) + timedelta(seconds=190)
    worker.rate.pause.assert_awaited_once_with(200)


async def test_budget_exhaustion_does_not_claim_complete_or_deactivate_absent_members(app):
    service = await setup(app)
    client = Client([response(types.User(7)), response()])
    await crawler(app, service, client, max_requests=2).run(await service.claim())
    progress = await service.progress(CHAT)
    assert (
        progress.stopped_because == "budget_exhausted"
        and not progress.converged
        and not progress.is_complete
    )
    assert await app.pool.fetchval("SELECT is_active FROM group_memberships")


async def test_basic_roster_must_match_requested_chat(app):
    service = await setup(app, chat=-10)
    full = SimpleNamespace(
        chats=[],
        users=[types.User(7)],
        full_chat=SimpleNamespace(
            participants=types.ChatParticipants(
                11, [types.ChatParticipant(7, 7, datetime.now(UTC))], 1
            )
        ),
    )
    await crawler(app, service, Client(basic=full)).run(await service.claim())
    assert await app.pool.fetchval("SELECT count(*) FROM group_memberships") == 0
    assert (await service.progress(-10)).status == "paused"
