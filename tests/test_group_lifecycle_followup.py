"""Additional Telegram update forms and private crawl progress regressions."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import Button, errors, types
from test_admin_reference_database import app as app

from bot.admin_keyboard import reply_event
from bot.handlers.groups import GROUP_UPDATES, GroupTracker
from bot.keyboards.admin import button
from bot.premium_emoji import BotPresentation
from bot.roster_progress import RosterProgress, progress_text
from bot.texts.group_lifecycle import LAZY, announcement
from repositories.postgres.roster_crawl import RosterCrawlRepository
from schemas.group import GroupChange, GroupStanding, Standing
from services.roster_crawl import RosterCrawlService

NOW = datetime.now(UTC)


@pytest.mark.parametrize("snapshot", [False, True])
async def test_basic_group_modern_promotion_and_roster_snapshot(snapshot):
    participant = types.ChatParticipantAdmin(123, 7, NOW)
    update = (
        types.UpdateChatParticipants(types.ChatParticipants(77, [participant], 3))
        if snapshot
        else types.UpdateChatParticipant(77, NOW, 7, 123, 3, new_participant=participant)
    )
    assert type(update) in GROUP_UPDATES
    entity = types.Chat(77, "Group", types.ChatPhotoEmpty(), 5, NOW, 3)
    service = SimpleNamespace(record=AsyncMock(), get=AsyncMock(return_value=None))
    tracker = GroupTracker(SimpleNamespace(get_entity=AsyncMock(return_value=entity)), service, 123)
    await tracker.handle_update(update)
    change = service.record.call_args.args[0]
    assert change.chat_id == -77 and change.admin and change.active
    assert change.sequence == 3


async def test_forbidden_roster_does_not_invent_removal():
    service = SimpleNamespace(record=AsyncMock())
    tracker = GroupTracker(None, service, 123)
    await tracker.handle_update(types.UpdateChatParticipants(types.ChatParticipantsForbidden(77)))
    service.record.assert_not_awaited()


async def test_supergroup_permission_update_is_direct_and_waits_for_rights_to_settle():
    entity = types.Channel(77, "Group", types.ChatPhotoEmpty(), NOW, megagroup=True)
    service = SimpleNamespace(record=AsyncMock(), get=AsyncMock(return_value=None))
    probe = SimpleNamespace(
        check=AsyncMock(
            side_effect=[GroupStanding(Standing.MEMBER), GroupStanding(Standing.ADMIN, True)]
        )
    )
    tracker = GroupTracker(
        SimpleNamespace(get_entity=AsyncMock(return_value=entity)),
        service,
        123,
        probe=probe,
        settle_pause=0,
    )
    await tracker.handle_update(types.UpdateChannel(77))
    change = service.record.call_args.args[0]
    assert change.kind == "permissions_changed" and change.admin is True
    assert probe.check.await_count == 2


async def test_direct_permission_promotion_enqueues_crawl_and_direct_notice_atomically(app):
    app.groups.main_admin_id = 999
    app.groups.roster_enabled = True
    await app.groups.record(
        GroupChange(
            -10,
            "permissions_changed",
            "promote-followup",
            datetime.now(UTC),
            "group",
            active=True,
            admin=True,
            can_send=True,
            priority=0,
        )
    )
    crawl = await app.pool.fetchrow("SELECT status FROM group_roster_crawls WHERE chat_id=-10")
    assert crawl["status"] == "queued"
    payload = await app.pool.fetchval(
        "SELECT payload::text FROM outbox_events WHERE target_id=999 ORDER BY id DESC LIMIT 1"
    )
    import json

    assert json.loads(payload)["group_event"]["kind"] == "admin"
    roster = RosterCrawlService(RosterCrawlRepository(app.pool, app.bot), app.factory)
    assert (await roster.recent())[0]["chat_id"] == -10
    assert (await roster.summary())["pending"] == 1
    await roster.report_state({"owner": 999, "message_id": 22})
    assert await roster.report_state() == {"owner": 999, "message_id": 22}


def test_group_promotion_is_quiet_about_crawl_and_lazy_copy_does_not_guess_delivery():
    assert "بررسی می‌کنم" not in announcement("admin", {})
    assert "نرسیده" not in LAZY


@pytest.mark.parametrize("reply", [False, True])
async def test_premium_icon_slot_keeps_button_actions_labels_and_color(reply):
    presentation = BotPresentation()
    presentation.emojis = {"📥": 101, "⚙️": 102}
    button = (
        types.KeyboardButton(
            "📥 دریافت", types.ButtonTypeDefault(), types.KeyboardButtonStyle(bg_danger=True)
        )
        if reply
        else Button.inline("📥 دریافت", b"keep", style="danger")
    )
    original = button.to_dict()
    markup = types.ReplyKeyboardMarkup([types.KeyboardButtonRow([button])]) if reply else [[button]]
    sender = AsyncMock()
    await presentation._send(sender, "متن", {"buttons": markup})
    result = sender.call_args.kwargs["buttons"]
    actual = result.rows[0].buttons[0] if reply else result[0][0]
    assert actual.style.icon == 101 and actual.style.bg_danger
    assert actual.text == button.text and actual.type.to_dict() == button.type.to_dict()
    assert button.to_dict() == original


async def test_icon_rejection_retries_once_without_icons_or_losing_html_entities():
    presentation = BotPresentation()
    presentation.emojis = {"⚙️": 102}
    sender = AsyncMock(side_effect=[errors.PremiumAccountRequiredError(None), None])
    await presentation._send(
        sender,
        "متن",
        {
            "buttons": [[Button.inline("منو", "menu")]],
            "formatting_entities": [types.MessageEntityBold(0, 3)],
        },
    )
    assert sender.await_count == 2
    assert sender.call_args.kwargs["buttons"][0][0].style.icon is None
    assert isinstance(sender.call_args.kwargs["formatting_entities"][0], types.MessageEntityBold)


async def test_admin_adapter_adds_premium_to_direct_reply_and_dynamic_actions():
    presentation = BotPresentation()
    presentation.emojis = {"⚙️": 102}
    actual = SimpleNamespace(sender_id=7, chat_id=7, respond=AsyncMock())
    conversations = SimpleNamespace(update_data=AsyncMock(), get_data=AsyncMock(return_value={}))
    adapted = reply_event(actual, conversations, presentation)
    await adapted.respond(
        "تنظیمات",
        buttons=types.ReplyKeyboardMarkup(
            [types.KeyboardButtonRow([types.KeyboardButton("تنظیمات", types.ButtonTypeDefault())])]
        ),
    )
    assert actual.respond.call_args.kwargs["buttons"].rows[0].buttons[0].style.icon == 102
    await adapted.respond("عملیات", buttons=[[button(7, "عملیات", "stats")]])
    assert actual.respond.call_args.kwargs["buttons"].rows[0].buttons[0].style.icon == 102
    assert (
        conversations.update_data.call_args.kwargs["admin_reply_actions"]["عملیات"]
        == "adm:7:stats:0"
    )


def summary(pending=1, failed=0):
    return dict(
        total=1,
        pending=pending,
        running=pending,
        done=int(not pending and not failed),
        failed=failed,
        seen_unique=20,
        stored_new=15,
        skipped_accounts=2,
    )


async def test_private_report_reuses_durable_message_and_stays_running_during_pause():
    saved = {}

    async def state(value=None):
        if value is not None:
            saved.update(value)
        return saved

    service = SimpleNamespace(
        summary=AsyncMock(return_value=summary()),
        recent=AsyncMock(return_value=[]),
        report_state=state,
    )
    client = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(id=22)),
        edit_message=AsyncMock(return_value=SimpleNamespace(id=22)),
    )
    report = RosterProgress(client, service, 7, interval=0)
    await report.update(active=True)
    assert saved == {"owner": 7, "message_id": 22}
    assert "در حال" in client.send_message.call_args.args[1]
    service.summary.return_value = summary(0)
    await report.update()
    client.send_message.assert_awaited_once()
    assert client.edit_message.call_args.args[:2] == (7, 22)
    assert "تمام شد" in client.edit_message.call_args.args[2]
    assert "در حال" in progress_text(summary(), [])
    assert "تمام شد" not in progress_text(summary(0, 1), [])


async def test_progress_send_failure_does_not_escape_into_crawling():
    service = SimpleNamespace(summary=AsyncMock(side_effect=OSError()))
    await RosterProgress(None, service, 7).update(active=True)
