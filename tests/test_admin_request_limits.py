"""Live roles, independent rate switches and bounded SQL admission under contention."""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_download_quota_database import app as app

from repositories.postgres.download_quota import DownloadQuotaRepository
from schemas.download_limits import NUMERIC_CONTROLS, TOGGLE_CONTROLS, DownloadLimits
from schemas.media import DownloadError
from services.limits import RequestLimiter


def quota(app):
    return DownloadQuotaRepository(app.pool, app.bot, main_admin_id=999)


async def enable_gaps(app):
    for action in ("user_gap_toggle", "group_gap_toggle"):
        await app.admin.mutate(999, action, value=True)


@pytest.mark.parametrize("owner", [999, 7])
async def test_root_and_delegated_admins_ignore_all_limits_and_spend_no_group_slots(app, owner):
    if owner != 999:
        await app.admin.mutate(999, "grant", owner)
    await enable_gaps(app)
    await app.admin.mutate(999, "group_quota", value=1)
    assert (await quota(app).claim(8, "inspect", chat_id=-10, request_id="regular"))[
        0
    ] == "accepted"
    instances = [quota(app) for _ in range(4)]
    results = await asyncio.gather(
        *(
            instances[i % 4].claim(owner, "inspect", chat_id=-10, request_id=str(i))
            for i in range(100)
        )
    )
    assert all(result == ("accepted", 0, 0) for result in results)
    assert all(
        result == ("accepted", 0, 0)
        for result in await asyncio.gather(
            *(quota(app).claim(owner, "download") for _ in range(100))
        )
    )
    assert (await quota(app).claim(owner, "inspect", chat_id=owner, request_id="private"))[
        0
    ] == "accepted"
    assert (
        await app.pool.fetchval(
            "SELECT count(*) FROM download_request_windows WHERE user_id=$1", owner
        )
        == 0
    )
    assert await app.pool.fetchval("SELECT cardinality(accepted_at) FROM group_request_limits") == 1
    rows = await app.pool.fetch(
        "SELECT chat_id,state FROM bot_conversations WHERE user_id=$1", owner
    )
    assert {(row["chat_id"], row["state"]) for row in rows} == {
        (-10, "INSPECTING"),
        (owner, "INSPECTING"),
    }


async def test_grant_revoke_ban_and_bot_scope_are_fresh_for_existing_repository(app):
    repository = quota(app)
    assert (await repository.claim(7, "download"))[0] == "accepted"
    assert (await repository.claim(7, "download"))[0] == "user_rate_limited"
    await app.admin.mutate(999, "grant", 7)
    assert (await repository.claim(7, "download")) == ("accepted", 0, 0)
    other_bot = DownloadQuotaRepository(app.pool, 778)
    assert (await other_bot.claim(7, "download"))[0] == "accepted"
    assert (await other_bot.claim(7, "download"))[0] == "user_rate_limited"
    await app.admin.mutate(999, "revoke", 7)
    assert (await repository.claim(7, "download"))[0] == "user_rate_limited"
    await app.admin.mutate(999, "grant", 7)
    await app.admin.mutate(999, "ban", 7)
    with pytest.raises(DownloadError) as error:
        await app.admin.check_download(7)
    assert error.value.code == "user_banned"
    assert (await repository.claim(7, "download"))[0] == "user_rate_limited"


async def test_telegram_group_admin_is_not_an_installation_admin(app):
    await app.pool.execute("UPDATE bot_groups SET owner_id=7,admin_ids='[8]'::jsonb")
    for owner in (7, 8):
        assert (await quota(app).claim(owner, "download"))[0] == "accepted"
        assert (await quota(app).claim(owner, "download"))[0] == "user_rate_limited"


@pytest.mark.parametrize("owner", [999, 7])
@pytest.mark.parametrize(
    "restriction,code",
    [
        ("is_active=false", "group_unavailable"),
        ("can_send=false", "group_send_forbidden"),
        ("migrated_to=-20", "group_unavailable"),
    ],
)
async def test_admin_rate_exemption_preserves_group_permissions(app, owner, restriction, code):
    if owner != 999:
        await app.admin.mutate(999, "grant", owner)
    await app.pool.execute(f"UPDATE bot_groups SET {restriction} WHERE chat_id=-10")
    with pytest.raises(DownloadError) as error:
        await quota(app).claim(owner, "inspect", chat_id=-10, request_id="denied")
    assert error.value.code == code
    assert await app.pool.fetchval("SELECT count(*) FROM bot_conversations") == 0


async def test_exempt_link_registration_remains_transactional(app, monkeypatch):
    from repositories.postgres.workflow import WorkflowRepository

    monkeypatch.setattr(
        WorkflowRepository, "record_admitted_link", AsyncMock(side_effect=RuntimeError("injected"))
    )
    with pytest.raises(RuntimeError):
        await quota(app).claim(999, "inspect", chat_id=-10, request_id="failed")
    for table in (
        "download_request_windows",
        "group_request_limits",
        "bot_users",
        "bot_conversations",
    ):
        assert await app.pool.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_user_gap_survives_restart_and_does_not_block_first_quality(app):
    await app.admin.mutate(999, "user_quota_toggle", value=False)
    await app.admin.mutate(999, "user_gap", value=120)
    await app.admin.mutate(999, "user_gap_toggle", value=True)
    assert (await quota(app).claim(7, "inspect", chat_id=7, request_id="first"))[0] == "accepted"
    assert (await quota(app).claim(7, "download"))[0] == "accepted"
    for chat in (7, -10, -20):
        result = await quota(app).claim(7, "inspect", chat_id=chat, request_id="next")
        assert result[0] == "user_gap_limited" and 115 <= result[1] <= 120
    assert (await quota(app).claim(7, "download"))[0] == "user_gap_limited"
    await app.pool.execute(
        "UPDATE download_request_windows SET accepted_at=ARRAY[clock_timestamp()-interval '61 seconds']"
    )
    reason, wait, interval = await quota(app).claim(7, "download")
    assert reason == "user_gap_limited" and 55 <= wait <= 60 and interval == 120
    await app.pool.execute(
        "UPDATE download_request_windows SET accepted_at=ARRAY[clock_timestamp()-interval '121 seconds']"
    )
    assert (await quota(app).claim(7, "download"))[0] == "accepted"


async def test_group_gap_is_independent_and_denial_spends_no_user_slot(app):
    await app.admin.mutate(999, "group_quota_toggle", value=False)
    await app.admin.mutate(999, "group_gap", value=120)
    await app.admin.mutate(999, "group_gap_toggle", value=True)
    assert (await quota(app).claim(7, "inspect", chat_id=-10, request_id="one"))[0] == "accepted"
    limiter = RequestLimiter(quota_repository=quota(app))
    with pytest.raises(DownloadError, match="فاصله.*گروه.*120 ثانیه") as error:
        await limiter.check_link(8, -10, "two")
    assert error.value.code == "group_gap_limited" and "تهران" in str(error.value)
    assert (
        await app.pool.fetchval("SELECT count(*) FROM download_request_windows WHERE user_id=8")
        == 0
    )
    assert (await quota(app).claim(8, "inspect", chat_id=-20, request_id="other"))[0] == "accepted"
    await app.pool.execute(
        "UPDATE group_request_limits SET accepted_at=ARRAY[clock_timestamp()-interval '61 seconds']"
    )
    assert (await quota(app).claim(9, "inspect", chat_id=-10, request_id="still-gap"))[
        0
    ] == "group_gap_limited"
    await app.admin.mutate(999, "group_gap_toggle", value=False)
    assert (await quota(app).claim(9, "inspect", chat_id=-10, request_id="gap-off"))[
        0
    ] == "accepted"
    await app.admin.mutate(999, "group_quota_toggle", value=True)
    await app.admin.mutate(999, "limit", -10, 1)
    assert (await quota(app).claim(10, "inspect", chat_id=-10, request_id="quota"))[
        0
    ] == "group_rate_limited"


async def test_switches_preserve_numbers_and_apply_immediately_without_process_cache(app):
    repository = quota(app)
    await app.admin.mutate(999, "user_quota_toggle", value=False)
    for _ in range(3):
        assert (await repository.claim(7, "download"))[0] == "accepted"
    await app.admin.mutate(999, "user_quota", value=2)
    options = await app.admin.options(999)
    assert options["user_links_per_minute"] == 2 and options["user_quota_enabled"] is False
    await app.admin.mutate(999, "user_quota_toggle", value=True)
    assert (await repository.claim(7, "download"))[0] == "user_rate_limited"
    await app.admin.mutate(999, "user_quota_toggle", value=False)
    await app.admin.mutate(999, "user_gap_toggle", value=True)
    with pytest.raises(DownloadError, match="فاصله.*5 ثانیه"):
        await RequestLimiter(quota_repository=repository).check(7, "download")
    await app.admin.mutate(999, "user_gap_toggle", value=False)
    assert (await repository.claim(7, "download"))[0] == "accepted"
    values = await repository.limits()
    assert (
        values["user_limit"] == 2
        and not values["user_quota_enabled"]
        and not values["user_gap_enabled"]
    )


async def test_disabled_quotas_allow_thousand_requests_without_unbounded_timestamp_arrays(app):
    for action in ("user_quota_toggle", "group_quota_toggle"):
        await app.admin.mutate(999, action, value=False)
    instances = [quota(app) for _ in range(4)]
    results = await asyncio.gather(
        *(instances[i % 4].claim(7, "inspect", chat_id=-10, request_id=str(i)) for i in range(1005))
    )
    assert all(row[0] == "accepted" for row in results)
    for table in ("download_request_windows", "group_request_limits"):
        assert await app.pool.fetchval(f"SELECT cardinality(accepted_at) FROM {table}") == 1000
    await app.admin.mutate(999, "user_quota_toggle", value=True)
    await app.admin.mutate(999, "user_quota", value=1000)
    assert (await quota(app).claim(7, "inspect", chat_id=-10, request_id="reenabled"))[
        0
    ] == "user_rate_limited"


async def test_group_gap_serializes_different_users_across_repository_instances(app):
    await app.admin.mutate(999, "group_gap_toggle", value=True)
    await app.admin.mutate(999, "group_gap", value=120)
    results = await asyncio.gather(
        *(quota(app).claim(i, "inspect", chat_id=-10, request_id=str(i)) for i in range(1, 101))
    )
    assert sum(row[0] == "accepted" for row in results) == 1
    assert sum(row[0] == "group_gap_limited" for row in results) == 99
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 1


async def test_user_gap_serializes_concurrent_cross_chat_attempts(app):
    await app.admin.mutate(999, "user_quota_toggle", value=False)
    await app.admin.mutate(999, "user_gap_toggle", value=True)
    results = await asyncio.gather(
        *(
            quota(app).claim(7, "inspect", chat_id=(7, -10, -20)[i % 3], request_id=str(i))
            for i in range(100)
        )
    )
    assert sum(row[0] == "accepted" for row in results) == 1
    assert sum(row[0] == "user_gap_limited" for row in results) == 99
    assert await app.pool.fetchval("SELECT count(*) FROM bot_conversations") == 1


@pytest.mark.parametrize("subject", ["user", "group"])
async def test_retry_uses_longer_of_quota_and_gap_instead_of_premature_retry(app, subject):
    await app.admin.mutate(999, "user_quota_toggle", value=subject == "user")
    await app.admin.mutate(999, "group_quota", value=1)
    await app.admin.mutate(999, f"{subject}_gap_toggle", value=True)
    assert (await quota(app).claim(7, "inspect", chat_id=-10, request_id="one"))[0] == "accepted"
    owner = 7 if subject == "user" else 8
    reason, remaining, limit = await quota(app).claim(
        owner, "inspect", chat_id=-10, request_id="two"
    )
    assert reason == f"{subject}_rate_limited" and 55 <= remaining <= 60 and limit == 1
    await app.admin.mutate(999, f"{subject}_gap", value=120)
    reason, remaining, limit = await quota(app).claim(
        owner, "inspect", chat_id=-10, request_id="three"
    )
    assert reason == f"{subject}_gap_limited" and 115 <= remaining <= 120 and limit == 120


async def test_real_panel_inputs_and_switches_persist_in_sql(app):
    from test_download_settings import event, send

    from bot.handlers import admin_reference as panel
    from bot.state.storage import MemoryStateStorage
    from bot.texts import admin_buttons as b
    from core.enums import ConversationState as CS
    from services.conversation import ConversationService

    conversations = ConversationService(MemoryStateStorage())
    await panel.download_settings(event(), app.admin, conversations)
    for label, key in ((b.USER_GAP, "user_gap_seconds"), (b.GROUP_GAP, "group_gap_seconds")):
        await send(event(label), app.admin, conversations)
        await send(event("25"), app.admin, conversations)
        assert (await app.admin.options(999))[key] == 25
    for label, action in panel.RATE_TOGGLES.items():
        key = TOGGLE_CONTROLS[action]
        before = (await app.admin.options(999))[key]
        for expected in (not before, before):
            actual = event(label)
            await send(actual, app.admin, conversations)
            assert (await app.admin.options(999))[key] is expected
            assert await conversations.get_state(999, 999) is CS.ADMIN_DOWNLOAD_SETTINGS
            assert "ادمین‌های ثبت‌شده" in actual.respond.call_args.args[0]


@pytest.mark.parametrize("owner", [999, 7])
@pytest.mark.parametrize("chat", [7, -10])
async def test_actual_quality_handler_allows_repeated_admin_formats_but_keeps_button_owner(
    app, owner, chat
):
    from bot.handlers.quality import handle_quality
    from bot.state.menus import MenuStore
    from schemas.media import Media, Quality

    if owner != 999:
        await app.admin.mutate(999, "grant", owner)
    await enable_gaps(app)
    qualities = tuple(
        Quality(str(i), str(i), "mp3", 128, "mp3", "audio/mpeg", "progressive", "")
        for i in range(2)
    )
    media = Media("soundcloud", "track", "Title", "Artist", 5, "page", None, qualities)
    menus = MenuStore()
    token = await menus.create(owner, chat, media, message_id=42)
    repository = quota(app)
    repository.claim = AsyncMock(wraps=repository.claim)
    service = SimpleNamespace(deliver=AsyncMock())
    actual = SimpleNamespace(
        sender_id=owner,
        chat_id=chat,
        get_input_chat=AsyncMock(return_value="peer"),
        answer=AsyncMock(),
        respond=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())),
    )
    for index in range(2):
        actual.data = f"sc:{token}:{index}".encode()
        actual.pattern_match = re.match(rb"sc:([0-9a-f]{16}):([0-9]+)", actual.data)
        await handle_quality(
            actual,
            service,
            menus,
            limits=RequestLimiter(quota_repository=repository),
            policy=app.admin,
        )
    assert service.deliver.await_count == 2
    repository.claim.reset_mock()
    actual.sender_id = 999 if owner != 999 else 7
    await handle_quality(
        actual, service, menus, limits=RequestLimiter(quota_repository=repository), policy=app.admin
    )
    repository.claim.assert_not_awaited()
    assert service.deliver.await_count == 2 and actual.answer.call_args.kwargs["alert"]


@pytest.mark.parametrize("action", list(NUMERIC_CONTROLS) + list(TOGGLE_CONTROLS))
async def test_every_rate_control_is_root_only_validated_and_audited(app, action):
    await app.admin.mutate(999, "grant", 7)
    valid = True if action in TOGGLE_CONTROLS else 5
    with pytest.raises(DownloadError) as error:
        await app.admin.mutate(7, action, value=valid)
    assert error.value.code == "admin_denied"
    for invalid in (1, "true", None) if action in TOGGLE_CONTROLS else (0, True, "5", 3601):
        with pytest.raises(DownloadError):
            await app.admin.mutate(999, action, value=invalid)
    await app.admin.mutate(999, action, value=valid)
    key = (TOGGLE_CONTROLS if action in TOGGLE_CONTROLS else NUMERIC_CONTROLS)[action]
    assert (await app.admin.options(999))[key] == valid
    assert (
        await app.pool.fetchval("SELECT count(*) FROM admin_actions WHERE action=$1", action) == 1
    )


@pytest.mark.parametrize(
    "values",
    [
        {"user_quota_enabled": 1},
        {"group_gap_enabled": "false"},
        {"user_gap_seconds": 0},
        {"group_gap_seconds": 3601},
    ],
)
def test_invalid_persisted_rate_settings_fail_closed(values):
    with pytest.raises(ValueError):
        DownloadLimits.from_settings(values)
