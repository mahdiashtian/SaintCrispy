"""Real SQL concurrency, live configuration, restart and cross-chat quota regressions."""

import asyncio
import os
import uuid
from types import SimpleNamespace

import asyncpg
import pytest

from db.postgres.engine import migrate
from repositories.postgres.admin import AdminRepository
from repositories.postgres.download_quota import DownloadQuotaRepository
from repositories.postgres.group import GroupRepository
from repositories.postgres.unit_of_work import PostgresUnitOfWork
from schemas.media import DownloadError
from services.admin import AdminService
from services.group import GroupService
from services.limits import RequestLimiter


@pytest.fixture
async def app():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Dedicated PostgreSQL required")
    schema = "quota_test_" + uuid.uuid4().hex
    root = await asyncpg.connect(url)
    await root.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        url, min_size=1, max_size=16, server_settings={"search_path": schema}
    )
    try:
        await migrate(pool)
        bot = 777

        def factory():
            return PostgresUnitOfWork(pool, bot)

        groups = GroupService(GroupRepository(pool, bot), factory)
        admin = AdminService(AdminRepository(pool, bot), factory, groups, 999)
        await pool.execute("""INSERT INTO bot_groups (telegram_account_id,chat_id,chat_kind,is_active)
                            VALUES (777,-10,'supergroup',true),(777,-20,'supergroup',true)""")
        yield SimpleNamespace(
            pool=pool, bot=bot, admin=admin, quota=DownloadQuotaRepository(pool, bot), schema=schema
        )
    finally:
        await pool.close()
        await root.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await root.close()


async def test_atomic_multi_process_clicks_use_configured_limit_and_do_not_reset_on_restart(app):
    await app.admin.mutate(999, "user_quota", value=3)
    repositories = [DownloadQuotaRepository(app.pool, app.bot) for _ in range(4)]
    results = await asyncio.gather(*(repositories[i % 4].claim(7, "download") for i in range(100)))
    assert sum(row[0] == "accepted" for row in results) == 3
    assert all(1 <= row[1] <= 60 for row in results if row[0] != "accepted")
    assert (await DownloadQuotaRepository(app.pool, app.bot).claim(7, "download"))[
        0
    ] == "user_rate_limited"
    assert (await DownloadQuotaRepository(app.pool, 778).claim(7, "download"))[0] == "accepted"


async def test_link_then_first_format_allowed_but_another_format_and_new_link_are_blocked(app):
    limiter = RequestLimiter(quota_repository=app.quota)
    assert await limiter.check_link(7, 7, "first")
    await limiter.check(7, "download")
    with pytest.raises(DownloadError, match="ثانیه.*صبر") as error:
        await limiter.check(7, "download")
    assert error.value.code == "user_rate_limited" and "تهران" in str(error.value)
    for chat in (7, -10, -20):
        with pytest.raises(DownloadError):
            await limiter.check_link(7, chat, "another")
    assert await app.pool.fetchval("SELECT count(*) FROM bot_conversations") == 1


async def test_group_shared_cap_user_denial_does_not_spend_group_slot_and_override_is_live(app):
    await app.admin.mutate(999, "group_quota", value=2)
    assert (await app.quota.claim(1, "inspect", chat_id=-10, request_id="1"))[0] == "accepted"
    assert (await app.quota.claim(1, "inspect", chat_id=-10, request_id="duplicate"))[
        0
    ] == "user_rate_limited"
    assert (await app.quota.claim(2, "inspect", chat_id=-10, request_id="2"))[0] == "accepted"
    assert (await app.quota.claim(3, "inspect", chat_id=-10, request_id="3"))[
        0
    ] == "group_rate_limited"
    await app.admin.mutate(999, "limit", -10, 3)
    assert (await app.quota.claim(3, "inspect", chat_id=-10, request_id="3"))[0] == "accepted"
    await app.admin.mutate(999, "group_quota", value=1)
    assert (await app.quota.claim(4, "inspect", chat_id=-20, request_id="4"))[0] == "accepted"
    assert (await app.quota.claim(5, "inspect", chat_id=-20, request_id="5"))[
        0
    ] == "group_rate_limited"


async def test_quota_changes_apply_immediately_and_rolling_expiration_is_not_a_fixed_minute(app):
    assert (await app.quota.claim(7, "download"))[0] == "accepted"
    await app.admin.mutate(999, "user_quota", value=2)
    assert (await app.quota.claim(7, "download"))[0] == "accepted"
    await app.admin.mutate(999, "user_quota", value=1)
    assert (await app.quota.claim(7, "download"))[0] == "user_rate_limited"
    await app.pool.execute(
        "UPDATE download_request_windows SET accepted_at=ARRAY[clock_timestamp()-interval '61 seconds']"
    )
    assert (await app.quota.claim(7, "download"))[0] == "accepted"


@pytest.mark.parametrize("value", [0, 1001, True, "5", -1])
async def test_invalid_or_non_root_configuration_never_changes_settings(app, value):
    with pytest.raises(DownloadError):
        await app.admin.mutate(999, "user_quota", value=value)
    with pytest.raises(DownloadError):
        await app.admin.mutate(7, "group_quota", value=5)
    assert await app.pool.fetchval("SELECT count(*) FROM app_settings") == 0


async def test_legacy_cooldown_is_adopted_without_erasing_existing_history(app):
    await app.pool.execute("DELETE FROM app_schema_migrations WHERE version=13")
    await app.pool.execute("INSERT INTO request_limits VALUES (777,8,'download',clock_timestamp())")
    await migrate(app.pool)
    assert (await app.quota.claim(8, "download"))[0] == "user_rate_limited"
    assert await app.pool.fetchval("SELECT count(*) FROM request_limits") == 1


async def test_actual_quality_handler_answers_second_format_without_second_delivery(app):
    import re
    from unittest.mock import AsyncMock

    from bot.handlers.quality import handle_quality
    from bot.state.menus import MenuStore
    from schemas.media import Media, Quality

    qualities = tuple(
        Quality(str(i), str(i), "mp3", 128, "mp3", "audio/mpeg", "progressive", "")
        for i in range(2)
    )
    media = Media("soundcloud", "track", "Title", "Artist", 5, "page", None, qualities)
    menus, limiter = MenuStore(), RequestLimiter(quota_repository=app.quota)
    await limiter.check_link(7, 7, "link")
    token = await menus.create(7, 7, media, message_id=42)
    service = SimpleNamespace(deliver=AsyncMock())
    actual = SimpleNamespace(
        sender_id=7,
        chat_id=7,
        get_input_chat=AsyncMock(return_value="peer"),
        answer=AsyncMock(),
        respond=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())),
    )
    for index in range(2):
        actual.data = f"sc:{token}:{index}".encode()
        actual.pattern_match = re.match(rb"sc:([0-9a-f]{16}):([0-9]+)", actual.data)
        await handle_quality(actual, service, menus, limits=limiter)
    service.deliver.assert_awaited_once()
    assert actual.answer.call_args.kwargs["alert"]
    assert "ثانیه" in actual.answer.call_args.args[0]


async def test_failed_link_registration_rolls_back_both_quotas_and_state(app, monkeypatch):
    from unittest.mock import AsyncMock

    from repositories.postgres.workflow import WorkflowRepository

    original = WorkflowRepository.record_admitted_link
    monkeypatch.setattr(
        WorkflowRepository, "record_admitted_link", AsyncMock(side_effect=RuntimeError("injected"))
    )
    with pytest.raises(RuntimeError):
        await app.quota.claim(7, "inspect", chat_id=-10, request_id="failed")
    for table in (
        "download_request_windows",
        "group_request_limits",
        "bot_users",
        "bot_conversations",
    ):
        assert await app.pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    monkeypatch.setattr(WorkflowRepository, "record_admitted_link", original)
    assert (await app.quota.claim(7, "inspect", chat_id=-10, request_id="accepted"))[
        0
    ] == "accepted"


async def test_link_rate_rejection_replies_to_original_message_with_retry_time(app):
    from unittest.mock import AsyncMock

    from bot.handlers.limited import LimitedHandlers

    registered = []
    limiter = RequestLimiter(quota_repository=app.quota)
    wrapper = LimitedHandlers(
        SimpleNamespace(add_event_handler=lambda callback, builder: registered.append(callback)),
        limiter,
    )
    provider = AsyncMock()
    wrapper.add_event_handler(provider, None)
    actual = SimpleNamespace(
        sender_id=7, chat_id=7, id=42, raw_text="https://example.com/media", respond=AsyncMock()
    )
    await registered[0](actual)
    await registered[0](actual)
    provider.assert_awaited_once()
    assert actual.respond.call_args.kwargs["reply_to"] == 42
    assert (
        "ثانیه" in actual.respond.call_args.args[0] and "تهران" in actual.respond.call_args.args[0]
    )
