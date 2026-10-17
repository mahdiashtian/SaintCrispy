"""Native Couplyo-shaped panels, live cleanup filters and durable deletion guards."""

import asyncio
from itertools import product
from unittest.mock import AsyncMock

import pytest
from telethon import events, types
from test_admin_reference_database import app as app
from test_admin_reference_database import member
from test_download_settings import event

from bot.admin_keyboard import reply_event
from bot.handlers import admin as ui
from bot.handlers import admin_reference as panel
from bot.handlers.admin_maintenance import cleanup_callback, open_cleanup
from bot.keyboards import admin_reply
from bot.keyboards.cleanup import builder
from bot.texts import admin_buttons as labels
from core.cleanup import ENTITIES, decode, encode, filters
from core.enums import ConversationState as CS
from schemas.media import DownloadError
from services.admin_maintenance import cleanup_query
from services.conversation import ConversationService


def controls(actual, *, edited=False):
    return (actual.edit if edited else actual.respond).call_args.kwargs["buttons"]


def action_button(rows, action):
    return next(
        item for row in rows for item in row if item.type.data.split(b":")[2] == action.encode()
    )


async def click(item, app, *, owner=999, conversations=None):
    actual = event(owner=owner)
    actual.data = item.type.data
    await ui.callback(actual, app.admin, conversations=conversations)
    return actual


@pytest.mark.parametrize("ent", ENTITIES, ids=lambda item: item.name)
def test_every_mask_combination_round_trips_and_builds_a_valid_query(ent):
    for masks in product(*(range(attr.full + 1) for attr in ent.attributes)):
        assert decode(encode(ent, masks)) == (ent, masks)
        cleanup_query(ent.name, filters(ent, masks), 999)
        for row in builder(999, ent, masks, deletable=0 not in masks):
            for item in row:
                assert len(item.type.data) <= 64
                bytes(item)


@pytest.mark.parametrize("raw", ["", "u", "u_33", "u_33v", "u_33f_bad", "g_3333", "x_333"])
def test_malformed_filter_does_not_fall_back_to_a_broader_selection(raw):
    with pytest.raises(DownloadError):
        decode(raw)


async def test_all_category_menus_remain_reply_keyboards_and_filter_picker_is_inline(app):
    conversations = ConversationService(app.workflow)
    for state in admin_reply.MENUS:
        actual = event()
        await panel.panel(reply_event(actual, conversations), app.admin, conversations, CS(state))
        assert isinstance(controls(actual), types.ReplyKeyboardMarkup)
    await conversations.set_state(999, 999, CS.ADMIN_CAT_CLEANUP_PANEL)
    actual = event(labels.FILTER_CLEANUP)
    with pytest.raises(events.StopPropagation):
        await panel.handle_message(actual, app.admin, conversations)
    rows = controls(actual)
    assert [row[0].text for row in rows] == ["👥 پاکسازی کاربران", "🏠 پاکسازی گروه‌ها", "✖️ بستن"]
    assert all(
        isinstance(item.type, types.InlineButtonTypeCallback) for row in rows for item in row
    )


async def test_cleanup_preview_confirm_back_close_and_replayed_clicks_on_real_sql(app):
    for identity in (7, 8, 9, 999):
        await app.workflow.touch_user(identity)
    await app.membership.persist(member(app))
    await app.admin.mutate(999, "grant", 9)
    conversations = ConversationService(app.workflow)
    actual = event()
    await open_cleanup(reply_event(actual, conversations), app.admin)
    selected = await click(controls(actual)[0][0], app, conversations=conversations)
    selected = await click(
        action_button(controls(selected, edited=True), "cleanall"), app, conversations=conversations
    )
    rows = controls(selected, edited=True)
    assert "۱" in selected.edit.call_args.args[0]
    prepared = await click(action_button(rows, "cleango"), app, conversations=conversations)
    assert await app.pool.fetchval("SELECT count(*) FROM bot_users") == 4
    confirm_rows = controls(prepared, edited=True)
    assert [item.text for item in confirm_rows[0]] == ["❌ نه، برگرد", "✅ بله، حذف کن"]
    back = await click(confirm_rows[0][0], app, conversations=conversations)
    assert action_button(controls(back, edited=True), "cleango")
    foreign = await click(confirm_rows[0][1], app, owner=9, conversations=conversations)
    assert foreign.answer.call_args.kwargs["alert"]
    results = await asyncio.gather(
        *(click(confirm_rows[0][1], app, conversations=conversations) for _ in range(10))
    )
    assert sum(result.edit.await_count for result in results) == 1
    assert {row["user_id"] for row in await app.pool.fetch("SELECT user_id FROM bot_users")} == {
        7,
        9,
        999,
    }
    closed = await click(
        action_button(controls(back, edited=True), "cleanclose"), app, conversations=conversations
    )
    assert controls(closed, edited=True) is None
    assert "هیچی حذف نشد" in closed.edit.call_args.args[0]


async def test_empty_selection_has_no_delete_button_and_cannot_prepare_a_token(app):
    actual = event()
    await cleanup_callback(actual, app.admin, "cleanfilter", "u_00f")
    rows = controls(actual, edited=True)
    assert not any(item.type.data.split(b":")[2] == b"cleango" for row in rows for item in row)
    with pytest.raises(DownloadError):
        await cleanup_callback(event(), app.admin, "cleango", "u_00f")
    assert await app.pool.fetchval("SELECT count(*) FROM admin_requests") == 0


async def test_partial_account_kind_selection_uses_exact_values_in_sql(app):
    for identity, kind in enumerate(("real", "unknown", "bot", "deleted"), 20):
        await app.workflow.touch_user(identity)
        await app.pool.execute(
            "UPDATE bot_users SET account_kind=$2 WHERE user_id=$1", identity, kind
        )
    ent, masks = decode("u_335")
    selected = filters(ent, masks)
    preview = await app.admin.maintenance.cleanup_preview(999, ent.name, selected)
    assert preview["matched"] == 2 and preview["total"] == 4
    token, _ = await app.admin.maintenance.prepare_cleanup(999, ent.name, selected)
    assert await app.admin.maintenance.confirm_cleanup(999, token) == 2
    assert {
        row["account_kind"] for row in await app.pool.fetch("SELECT account_kind FROM bot_users")
    } == {"unknown", "deleted"}


async def test_delegated_admin_cannot_open_cleanup_or_use_root_filter_buttons(app):
    await app.workflow.touch_user(9)
    await app.admin.mutate(999, "grant", 9)
    actual = event(owner=9)
    with pytest.raises(DownloadError):
        await panel.panel(
            actual, app.admin, ConversationService(app.workflow), CS.ADMIN_CAT_CLEANUP_PANEL
        )
    actual.data = b"adm:9:cleanfilter:u_33f"
    await ui.callback(actual, app.admin, conversations=ConversationService(app.workflow))
    assert actual.answer.call_args.kwargs["alert"]
    actual.edit.assert_not_awaited()


async def test_group_refresh_requeries_directory_without_enqueuing_health_job(app):
    conversations = ConversationService(app.workflow)
    app.admin.prepare = AsyncMock()
    actual = event()
    actual.data = b"adm:999:dirrefresh:all_date"
    await ui.callback(actual, app.admin, conversations=conversations)
    assert len(controls(actual, edited=True)[-1]) == 1
    app.admin.prepare.assert_not_awaited()
    assert await app.pool.fetchval("SELECT count(*) FROM admin_requests") == 0


async def test_two_open_panels_keep_their_own_filter_snapshot_and_reject_expired_confirmation(app):
    await app.workflow.touch_user(8)
    first, second = event(), event()
    await cleanup_callback(first, app.admin, "cleanfilter", "u_33f")
    await cleanup_callback(second, app.admin, "cleanfilter", "g_333")
    prepared = await click(action_button(controls(first, edited=True), "cleango"), app)
    item = controls(prepared, edited=True)[0][1]
    await app.pool.execute(
        "UPDATE admin_requests SET expires_at=clock_timestamp()-interval '1 second'"
    )
    expired = await click(item, app, conversations=ConversationService(app.workflow))
    assert expired.answer.call_args.kwargs["alert"]
    assert await app.admin.user(999, 8) is not None


async def test_watch_list_switches_and_removal_edit_their_native_panel(app):
    channel = {"id": -100077, "title": "Archive", "link": "https://t.me/test_archive"}
    await app.admin.channels.upsert_watch(
        999, {"chat_id": -10, "channel": channel, "kinds": ["text"]}
    )
    conversations = ConversationService(app.workflow)
    await conversations.set_state(999, 999, CS.ADMIN_WATCH_PANEL)
    actual = event(labels.WATCH_LIST)
    with pytest.raises(events.StopPropagation):
        await panel.handle_message(actual, app.admin, conversations)
    detail = await click(action_button(controls(actual), "watch"), app, conversations=conversations)
    switched = await click(
        action_button(controls(detail, edited=True), "wkind"), app, conversations=conversations
    )
    assert (await app.admin.channels.get(999))["watches"][0]["kinds"] == []
    prompt = await click(
        action_button(controls(switched, edited=True), "watchdel"), app, conversations=conversations
    )
    removed = await click(
        action_button(controls(prompt, edited=True), "watchconfirm"),
        app,
        conversations=conversations,
    )
    assert (await app.admin.channels.get(999))["watches"] == []
    removed.edit.assert_awaited_once()
    removed.respond.assert_not_awaited()


async def test_audience_inline_picker_enters_the_reply_exclusions_form(app):
    conversations = ConversationService(app.workflow)
    await conversations.set_state(999, 999, CS.ADMIN_CAT_MSG_PANEL)
    actual = event(labels.GROUP_BROADCAST)
    with pytest.raises(events.StopPropagation):
        await panel.handle_message(actual, app.admin, conversations)
    rows = controls(actual)
    assert len(rows) == 4 and all(
        isinstance(item.type, types.InlineButtonTypeCallback) for row in rows for item in row
    )
    reply = await click(rows[1][0], app, conversations=conversations)
    assert isinstance(controls(reply), types.ReplyKeyboardMarkup)
    state, data = await conversations.get(999, 999)
    assert state is CS.ADMIN_INPUT and data["action"] == "exclusions"
    assert data["group_audience"] == "admin"


async def test_daily_history_keeps_both_boundary_controls_visible_and_noop_is_read_only(app):
    conversations = ConversationService(app.workflow)
    actual = event()
    actual.data = b"adm:999:stats_history:0"
    await ui.callback(actual, app.admin, conversations=conversations)
    rows = controls(actual, edited=True)
    assert [item.text for item in rows[0]] == ["◀️ قبلی", "بعدی ▶️"]
    for item in rows[0]:
        result = await click(item, app, conversations=conversations)
        result.edit.assert_not_awaited()
        result.respond.assert_not_awaited()


async def test_native_callbacks_recheck_a_delegated_admin_after_revocation(app):
    await app.workflow.touch_user(9)
    await app.admin.mutate(999, "grant", 9)
    actual = event(owner=9)
    actual.data = b"adm:9:stats:0"
    conversations = ConversationService(app.workflow)
    await ui.callback(actual, app.admin, conversations=conversations)
    actual.edit.assert_awaited_once()
    await app.admin.mutate(999, "revoke", 9)
    actual.edit.reset_mock()
    await ui.callback(actual, app.admin, conversations=conversations)
    actual.edit.assert_not_awaited()
    assert actual.answer.call_args.kwargs["alert"]
