"""Canonical admin copy keeps HTML, premium entities and safe legacy restores."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types
from telethon.helpers import add_surrogate
from test_couplyo_admin_reference import admin_fake, event, seed_member
from test_couplyo_admin_reference import sql_app as sql_app

from bot.handlers import admin as admin_ui
from bot.handlers import admin_reference as panel
from bot.handlers.admin_render import source_reply, split_entities
from bot.management import ManagementHooks
from bot.state.storage import MemoryStateStorage
from bot.texts import admin_buttons as b
from bot.texts import couplyo_admin as source_copy
from core.enums import ConversationState as CS
from schemas.admin import PendingEffect
from services.conversation import ConversationService
from services.management import ManagementService


async def test_html_and_premium_entities_share_parsed_utf16_offsets():
    actual = event()
    presentation = SimpleNamespace(
        disabled=False,
        prepare=lambda text: (text, []),
        entities=lambda text: [types.MessageEntityCustomEmoji(0, 2, 123)],
    )
    await source_reply(
        actual,
        '📬 <b>گروه &amp; کاربر</b> <a href="tg://user?id=7">نام</a>',
        presentation=presentation,
    )
    text = actual.respond.call_args.args[0]
    entities = actual.respond.call_args.kwargs["formatting_entities"]
    assert "<b>" not in text and "&amp;" not in text and "&" in text
    assert any(isinstance(entity, types.MessageEntityBold) for entity in entities)
    assert any(
        isinstance(entity, types.MessageEntityTextUrl) and entity.url == "tg://user?id=7"
        for entity in entities
    )
    for entity in entities:
        assert 0 <= entity.offset < entity.offset + entity.length <= len(add_surrogate(text))
        bytes(entity)


def test_long_html_chunks_preserve_emoji_surrogates_and_clip_spans():
    text = "📬" * 5000
    chunks = split_entities(
        text, [types.MessageEntityBold(0, 10000), types.MessageEntityCustomEmoji(3998, 2, 123)]
    )
    assert "".join(chunk for chunk, _ in chunks) == text
    assert all(len(add_surrogate(chunk)) <= 4000 for chunk, _ in chunks)
    for chunk, entities in chunks:
        assert all(
            entity.offset + entity.length <= len(add_surrogate(chunk)) for entity in entities
        )


async def test_source_group_audience_precedes_exclusions_and_purge_is_live_job():
    admin = admin_fake()
    conversation = ConversationService(MemoryStateStorage())
    actual = event(b.GROUP_BROADCAST)
    await panel.menu_action(
        actual, admin, conversation, CS.ADMIN_CAT_MSG_PANEL, b.GROUP_BROADCAST, None
    )
    rows = actual.respond.call_args.kwargs["buttons"]
    assert [row[0].text for row in rows] == [
        "👥 همه‌ی گروه‌های فعال",
        "🛡 فقط جایی که ادمینیم",
        "👤 فقط جایی که ادمین نیستیم",
        "✖️ بی‌خیال",
    ]
    await panel.menu_action(
        actual, admin, conversation, CS.ADMIN_CAT_CLEANUP_PANEL, b.PURGE_USERS, None
    )
    admin.prepare.assert_awaited_once_with(999, "purge_users", {})


async def test_legacy_public_backup_defaults_only_known_optional_columns(sql_app):
    app = sql_app
    await seed_member(app)
    content = json.loads((await app.admin.maintenance.backup(999)).data)
    for user in content["tables"]["bot_users"]:
        for key in ("last_active_at", "start_source", "start_chat_id"):
            user.pop(key)
    for row in content["tables"]["membership_events"]:
        row.pop("observed_admin")
    token, _ = await app.admin.maintenance.prepare_restore(999, json.dumps(content).encode())
    await app.admin.maintenance.confirm_restore(999, token)
    validated = await app.admin.maintenance.validate(json.dumps(content).encode())
    assert all(
        user["last_active_at"] is None and user["start_source"] is None
        for user in validated["tables"]["bot_users"]
    )


@pytest.mark.parametrize(
    "sender",
    [
        types.User(id=7, bot=True, first_name="Bot"),
        types.Channel(id=9, title="Anonymous", photo=types.ChatPhotoEmpty(), date=None),
    ],
)
async def test_group_storage_accepts_bot_and_anonymous_senders_without_user_upsert(sender):
    service = SimpleNamespace(admin=SimpleNamespace(main_admin_id=999), enqueue_storage=AsyncMock())
    hook = ManagementHooks(AsyncMock(), service)
    actual = SimpleNamespace(
        out=False,
        is_group=True,
        sender=sender,
        chat_id=-10,
        id=7,
        chat=SimpleNamespace(title="Group"),
        message=SimpleNamespace(message="hello", media=None),
    )
    await hook.observe(actual)
    service.enqueue_storage.assert_awaited_once()
    identity = service.enqueue_storage.call_args.args[3]
    assert identity == (7 if isinstance(sender, types.User) else -1000000000009)


async def test_trace_contains_fresh_group_membership_activity_and_source_storage_identity(sql_app):
    app = sql_app
    await seed_member(app)
    data = await app.admin.public_trace(999, 7)
    assert data["profile"]["user_id"] == 7
    assert data["groups"][0]["chat_id"] == -10
    channel = {"id": -1000000000077, "link": "https://t.me/example", "title": "Archive"}
    await app.admin.channels.update(999, "storage", channel)
    await app.admin.channels.update(999, "storage_kinds", ["text"])
    assert (
        await ManagementService(app.admin, app.groups, app.bot).enqueue_storage(
            -10, 7, "text", -1000000000009, sender_name="Anonymous"
        )
        == 1
    )
    payload = json.loads(
        await app.pool.fetchval("SELECT payload FROM outbox_events WHERE kind='forward'")
    )
    assert "Anonymous" in payload["footer"] and "تعداد اعضا" in payload["footer"]
    assert "tg://user?id=-" not in payload["footer"]


async def test_refused_storage_route_is_bounded_expires_and_config_change_retries(
    sql_app, monkeypatch
):
    app = sql_app
    channel = {"id": -1000000000077, "link": "https://t.me/example", "title": "Archive"}
    await app.admin.channels.update(999, "storage", channel)
    await app.admin.channels.update(999, "storage_kinds", ["text"])
    service = ManagementService(app.admin, app.groups, app.bot)
    now = 1000.0
    monkeypatch.setattr("services.management.time.monotonic", lambda: now)
    service.refuse_route(-10, channel["id"])
    assert await service.enqueue_storage(-10, 7, "text", 7) == 0
    effect = PendingEffect(
        1,
        "forward",
        channel["id"],
        {"management": {"feature": "storage", "source_peer": -10, "kind": "text"}},
        1,
        None,
        1,
    )
    assert not await service.allowed(effect)
    now += 301
    assert not service.route_refused(-10, channel["id"])
    service.refuse_route(-10, channel["id"])
    app.admin.channels.invalidate()
    assert not service.route_refused(-10, channel["id"])
    assert await service.enqueue_storage(-10, 8, "text", 7) == 1
    for identity in range(10001):
        service.refuse_route(-identity, channel["id"])
    assert len(service.refused_routes) == 10000


async def test_full_history_callback_clamps_and_pages_all_saved_days():
    actual = event()
    actual.data = b"adm:999:stats_history:999"
    admin = admin_fake()
    admin.history_count = AsyncMock(return_value=40)
    admin.history = AsyncMock(return_value=[])
    await admin_ui.callback(actual, admin)
    admin.history.assert_awaited_once_with(999, limit=14, offset=28)
    buttons = actual.edit.call_args.kwargs["buttons"]
    assert buttons[0][0].text == "◀️ قبلی"


def test_list_and_detail_share_same_source_group_card_and_preserve_unknown():
    group = SimpleNamespace(
        chat_id=-10,
        title="A <group>",
        is_active=None,
        is_admin=None,
        can_send=None,
        member_count=None,
        link=None,
    )
    card = source_copy.group_card(group)
    body = source_copy.group_block(card)
    assert body in source_copy.groups_list_page([card], 0, 1, 1)
    assert body in source_copy.group_info_page(card)
    assert "نامشخص" in body and "خارج‌شده/حذف" not in body
    assert "&lt;group&gt;" in body
