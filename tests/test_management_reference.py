"""Source storage classifications, forced-join and durable scheduling evidence."""

import asyncio
import copy
import struct
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, events, types
from test_couplyo_admin_reference import seed_member
from test_couplyo_admin_reference import sql_app as sql_fixture

from bot.management import ManagementHooks, message_kind
from schemas.admin import PendingEffect
from schemas.media import DownloadError
from services.admin_channels import DEFAULTS
from services.management import ManagementService, render_report

sql_app = sql_fixture


def hooks(config=None, result=None):
    settings = copy.deepcopy(DEFAULTS)
    settings.update(config or {})
    admin = SimpleNamespace(
        main_admin_id=999,
        check_download=AsyncMock(),
        channels=SimpleNamespace(get=AsyncMock(return_value=settings)),
    )
    client = AsyncMock(
        return_value=result
        or SimpleNamespace(participant=types.ChannelParticipant(user_id=7, date=datetime.now(UTC)))
    )
    client.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(77, 12345))
    service = SimpleNamespace(admin=admin, enqueue_storage=AsyncMock())
    return ManagementHooks(client, service), service, client


CHANNEL = {"id": -1000000000077, "link": "https://t.me/test_channel", "title": "Channel"}


def test_link_preview_is_text_even_if_telethon_convenience_photo_exists():
    event = SimpleNamespace(
        message=SimpleNamespace(
            message="https://example.com", media=types.MessageMediaWebPage(types.WebPageEmpty(1))
        ),
        photo=object(),
    )
    assert message_kind(event) == "text"


@pytest.mark.parametrize(
    "media",
    [
        None,
        types.MessageMediaContact(
            phone_number="0", first_name="Name", last_name="", vcard="", user_id=7
        ),
        types.MessageMediaGeo(types.GeoPointEmpty()),
    ],
)
def test_empty_services_and_unsupported_media_are_not_stored(media):
    assert message_kind(SimpleNamespace(message=SimpleNamespace(media=media, message=""))) is None


async def test_storage_observer_never_copies_private_admin_uploads_or_own_messages():
    hook, service, _ = hooks()
    for value in ({"is_private": True, "is_group": False}, {"is_group": True, "out": True}):
        await hook.observe(SimpleNamespace(**value))
    service.enqueue_storage.assert_not_awaited()


async def test_storage_observer_records_only_real_incoming_group_source_reference():
    hook, service, _ = hooks()
    message = SimpleNamespace(media=None, message="Hello")
    await hook.observe(
        SimpleNamespace(
            id=12,
            chat_id=-10,
            sender=types.User(id=7, username="human"),
            is_group=True,
            message=message,
            chat=SimpleNamespace(title="Group"),
        )
    )
    assert service.enqueue_storage.call_args.args[:4] == (-10, 12, "text", 7)


async def test_forced_join_disabled_has_zero_telegram_requests():
    hook, _, client = hooks()
    await hook.check_download(7)
    client.assert_not_awaited()
    hook.admin.check_download.assert_awaited_once_with(7)


async def test_concurrent_same_user_checks_use_single_full_pass_then_fresh_recheck():
    hook, _, client = hooks({"forced_channels": [CHANNEL]})
    await asyncio.gather(*(hook.ensure_joined(7, "usage") for _ in range(50)))
    assert client.await_count == 1
    await hook.ensure_joined(7, "recheck", fresh=True)
    assert client.await_count == 2


async def test_missing_channel_membership_blocks_with_join_link():
    hook, _, client = hooks({"forced_channels": [CHANNEL]})
    client.side_effect = errors.UserNotParticipantError(None)
    with pytest.raises(DownloadError) as failure:
        await hook.check_download(7)
    assert failure.value.code == "mandatory_join" and CHANNEL["link"] in str(failure.value)
    assert not hook.positive


async def test_unverifiable_channel_matches_source_fail_open_without_pass_cache():
    hook, _, client = hooks({"forced_channels": [CHANNEL]})
    client.side_effect = errors.ChatAdminRequiredError(None)
    await hook.ensure_joined(7, "usage")
    await hook.ensure_joined(7, "usage")
    assert client.await_count == 2 and not hook.positive


async def test_fresh_public_channel_lookup_recovers_real_input_peer_without_zero_hash():
    hook, _, client = hooks({"forced_channels": [CHANNEL]})
    real = types.InputPeerChannel(77, 12345)

    async def entity(value):
        if value == CHANNEL["id"]:
            raise ValueError("cold session")
        return real if value == CHANNEL["link"] else types.InputPeerUser(7, 98765)

    client.get_input_entity.side_effect = entity
    await hook.ensure_joined(7, "usage")
    request = client.call_args.args[0]
    assert request.channel.access_hash == 12345


async def test_copied_recheck_button_does_not_run_telegram_check():
    hook, _, client = hooks({"forced_channels": [CHANNEL]})
    event = SimpleNamespace(data=b"mgjoin:999", sender_id=7, answer=AsyncMock())
    await hook.recheck(event)
    client.assert_not_awaited()
    assert event.answer.call_args.kwargs["alert"]


async def test_unknown_recheck_never_claims_membership_was_verified():
    hook, _, client = hooks({"forced_channels": [CHANNEL]})
    client.side_effect = errors.ChatAdminRequiredError(None)
    event = SimpleNamespace(data=b"mgjoin:7", sender_id=7, answer=AsyncMock())
    await hook.recheck(event)
    assert "بررسی کامل نشد" in event.answer.call_args.args[0]
    assert not hook.positive


async def test_disabled_management_thousand_events_load_one_snapshot_without_group_sql():
    from services.admin_channels import ChannelSettings

    admin = SimpleNamespace(repository=SimpleNamespace(setting=AsyncMock(return_value={})))
    admin.channels = ChannelSettings(admin)
    groups = SimpleNamespace(
        get=AsyncMock(side_effect=AssertionError("No group SQL on disabled path"))
    )
    service = ManagementService(admin, groups, 123)
    counts = await asyncio.gather(
        *(service.enqueue_storage(-10, identity + 1, "text", 7) for identity in range(1000))
    )
    assert counts == [0] * 1000
    admin.repository.setting.assert_awaited_once()
    groups.get.assert_not_awaited()


async def test_start_missing_join_shows_owner_bound_recheck_and_stops_start():
    hook, _, client = hooks({"forced_channels": [CHANNEL]})
    client.side_effect = errors.UserNotParticipantError(None)
    event = SimpleNamespace(sender_id=7, is_private=True, respond=AsyncMock())
    with pytest.raises(events.StopPropagation):
        await hook.check_start(event)
    assert event.respond.call_args.kwargs["buttons"][0][0].type.data == b"mgjoin:7"


async def test_group_start_never_checks_or_exposes_private_forced_join_channels():
    hook, _, client = hooks({"forced_channels": [CHANNEL]})
    actual = SimpleNamespace(sender_id=7, is_private=False, respond=AsyncMock())
    await hook.check_start(actual)
    client.assert_not_awaited()
    actual.respond.assert_not_awaited()


def test_weekly_report_is_real_bounded_png_with_visible_data_dimensions():
    result = render_report(
        {"members": 1200, "weekly_active": 43, "weekly_messages": 800, "messages": 20350}
    )
    assert result.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", result[16:24]) == (900, 480)
    assert len(result) < 100000


async def test_storage_dedupe_independent_routes_and_fresh_settings_revocation(sql_app):
    app = sql_app
    management = ManagementService(app.admin, app.groups, app.bot)
    await app.admin.channels.update(999, "storage", CHANNEL)
    await app.admin.channels.upsert_watch(
        999, {"chat_id": -10, "channel": CHANNEL, "kinds": ["text"]}
    )
    assert await management.enqueue_storage(-10, 7, "text", 8) == 1
    assert await management.enqueue_storage(-10, 7, "text", 8) == 0
    row = await app.pool.fetchrow(
        "SELECT id,kind,target_id,payload,random_id,job_id,attempt_count FROM outbox_events WHERE telegram_account_id=$1",
        app.bot,
    )
    import json

    effect = PendingEffect(**{**dict(row), "payload": json.loads(row["payload"])})
    await app.admin.channels.remove_watch(999, -10)
    assert await management.allowed(effect)
    await app.admin.channels.update(999, "storage", None)
    assert not await management.allowed(effect)


async def test_archives_only_completed_result_not_incoming_private_content(sql_app):
    app = sql_app
    management = ManagementService(app.admin, app.groups, app.bot)
    await app.admin.channels.update(999, "archive", CHANNEL)
    await app.admin.channels.update(999, "storage", CHANNEL)
    assert await management.enqueue_storage(999, 5, "text", 999, is_private=True) == 0
    assert await management.archive_download(-10, 7, -10, 70, "instagram", "abc", "720p")
    assert not await management.archive_download(-10, 7, -10, 70, "instagram", "abc", "720p")
    row = await app.pool.fetchrow(
        "SELECT payload FROM outbox_events WHERE telegram_account_id=$1", app.bot
    )
    assert '"feature": "archive"' in row["payload"]


async def test_archive_document_does_not_depend_on_expiring_source_message(sql_app):
    import json

    app = sql_app
    management = ManagementService(app.admin, app.groups, app.bot)
    await app.admin.channels.update(999, "archive", CHANNEL)
    document = {"document_id": 17, "access_hash": 99, "file_reference": "dGVzdA=="}
    assert await management.archive_download(
        -10, 7, -10, 70, "instagram", "abc", "720p", document=document, caption="Media caption"
    )
    row = await app.pool.fetchrow(
        "SELECT kind,payload FROM outbox_events WHERE telegram_account_id=$1", app.bot
    )
    payload = json.loads(row["payload"])
    assert row["kind"] == "archive_document" and payload["document"] == document
    assert "source_peer" not in payload and "source_id" not in payload
    assert "Media caption" in payload["caption"] and "درخواست‌کننده: 7" in payload["caption"]


async def test_scheduler_periods_are_durable_across_restart_and_settings_disable(sql_app):
    app = sql_app
    await app.admin.channels.update(
        999, "promo", {"enabled": True, "hours": 6, "text": "Download media"}
    )
    await app.admin.channels.update(999, "admin_nudge", {"enabled": True, "hours": 24})
    await app.admin.channels.update(999, "weekly_report", {"enabled": True})
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    await ManagementService(app.admin, app.groups, app.bot).tick(now)
    await ManagementService(app.admin, app.groups, app.bot).tick(now)
    rows = await app.pool.fetch("SELECT kind FROM admin_jobs WHERE telegram_account_id=$1", app.bot)
    assert sorted(row["kind"] for row in rows) == ["broadcast", "nudge_admin", "report"]
    await app.admin.channels.update(
        999, "promo", {"enabled": False, "hours": 6, "text": "Download media"}
    )
    effect = PendingEffect(1, "message", -10, {"management": {"feature": "promo"}}, 1, None, 1)
    assert not await ManagementService(app.admin, app.groups, app.bot).allowed(effect)


async def test_public_developer_credit_changes_immediately_after_setting_edit(sql_app):
    app = sql_app
    service = ManagementService(app.admin, app.groups, app.bot)
    assert await service.developer_credit() is None
    await app.admin.channels.update(999, "developer_id", 12345)
    assert await service.developer_credit() == "👨‍💻 توسعه‌دهنده: tg://user?id=12345"


async def test_weekly_report_reads_real_group_activity(sql_app):
    app = sql_app
    await seed_member(app)
    data, caption = await ManagementService(app.admin, app.groups, app.bot).report(-10)
    assert data.startswith(b"\x89PNG") and "پیام‌های هفته: 1" in caption
