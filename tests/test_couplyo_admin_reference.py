"""Reference-shaped menus, persistent state and real SQL maintenance contracts."""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from telethon import events, types

from bot.handlers import admin as ui
from bot.handlers import admin_reference as reference
from bot.handlers.admin_maintenance import DEFAULTS as CLEANUP_DEFAULTS
from bot.handlers.admin_maintenance import decode, encode
from bot.keyboards.admin import button, compact_id
from bot.keyboards.admin_reply import MENUS, menu
from bot.state.storage import MemoryStateStorage
from bot.texts import admin_buttons as b
from core.enums import ConversationState as CS
from db.postgres.engine import migrate
from repositories.postgres.admin import AdminRepository
from repositories.postgres.group import GroupRepository
from repositories.postgres.membership import MembershipRepository
from repositories.postgres.unit_of_work import PostgresUnitOfWork
from schemas.group import GroupChange
from schemas.media import DownloadError
from schemas.membership import MembershipEvent
from services.admin import AdminService
from services.admin_channels import ChannelSettings
from services.admin_maintenance import TABLES
from services.conversation import ConversationService
from services.group import GroupService


def event(raw="", owner=999, **kwargs):
    return SimpleNamespace(
        sender_id=owner,
        chat_id=owner,
        raw_text=raw,
        data=None,
        is_group=False,
        respond=AsyncMock(return_value=SimpleNamespace(id=42)),
        answer=AsyncMock(),
        edit=AsyncMock(),
        **kwargs,
    )


def admin_fake():
    return SimpleNamespace(
        authorize=AsyncMock(return_value=True),
        clear_prompts=AsyncMock(),
        prepare=AsyncMock(return_value="token"),
        mutate=AsyncMock(),
        channels=SimpleNamespace(
            get=AsyncMock(return_value={"developer_id": None}),
            update=AsyncMock(),
            modify=AsyncMock(),
        ),
    )


@pytest.mark.parametrize("state", list(MENUS))
def test_reference_reply_keyboards_have_stable_back_and_category_rows(state):
    keyboard = menu(state)
    assert isinstance(keyboard, types.ReplyKeyboardMarkup)
    assert keyboard.resize and not keyboard.persistent
    assert keyboard.rows[-1].buttons[0].text == (b.CANCEL if state == "ADMIN_PANEL" else b.BACK)
    assert not any(
        "کاپل" in item.text or "شیپ" in item.text for row in keyboard.rows for item in row.buttons
    )


async def test_admin_entry_stops_later_private_handler_from_resetting_new_panel():
    admin, conversation = admin_fake(), ConversationService(MemoryStateStorage())
    actual = event("/admin")
    with pytest.raises(events.StopPropagation):
        await ui.open_panel(actual, admin, conversations=conversation)
    assert await conversation.get_state(999, 999) is CS.ADMIN_PANEL
    assert isinstance(actual.respond.call_args.kwargs["buttons"], types.ReplyKeyboardMarkup)


async def test_registered_message_sequence_preserves_admin_state_then_routes_reply_category():
    from bot.handlers.dispatcher import register_handlers

    registrations = []
    client = SimpleNamespace(
        add_event_handler=lambda callback, builder: registrations.append((callback, builder))
    )
    admin, conversation = admin_fake(), ConversationService(MemoryStateStorage())
    register_handlers(client, None, None, conversations=conversation, admin=admin)

    async def dispatch(raw):
        actual = event(raw, is_private=True, is_reply=False, is_channel=False)
        actual.message = SimpleNamespace(out=False, message=raw, fwd_from=None)
        for callback, builder in registrations:
            if not isinstance(builder, events.NewMessage):
                continue
            builder.resolved = True
            match = builder.filter(actual)
            if asyncio.iscoroutine(match):
                match = await match
            if match:
                try:
                    await callback(actual)
                except events.StopPropagation:
                    break
        return actual

    actual = await dispatch("/admin")
    assert await conversation.get_state(999, 999) is CS.ADMIN_PANEL
    assert actual.respond.await_count == 1
    actual = await dispatch(b.GROUPS)
    assert await conversation.get_state(999, 999) is CS.ADMIN_GROUPS_PANEL
    assert actual.respond.await_count == 1
    await dispatch("/start")
    assert await conversation.get_state(999, 999) is CS.MAIN


async def test_category_navigation_and_cancel_use_persisted_conversation_service():
    storage = MemoryStateStorage()
    conversation, admin = ConversationService(storage), admin_fake()
    await conversation.set_state(999, 999, CS.ADMIN_PANEL)
    with pytest.raises(events.StopPropagation):
        await reference.handle_message(event(b.GROUPS), admin, conversation)
    restarted = ConversationService(storage)
    assert await restarted.get_state(999, 999) is CS.ADMIN_GROUPS_PANEL
    with pytest.raises(events.StopPropagation):
        await reference.handle_message(event(b.BACK), admin, restarted)
    assert await restarted.get_state(999, 999) is CS.ADMIN_PANEL
    with pytest.raises(events.StopPropagation):
        await reference.handle_message(event(b.CANCEL), admin, restarted)
    assert await restarted.get_state(999, 999) is CS.MAIN
    assert admin.clear_prompts.await_count == 2


@pytest.mark.parametrize("command", ["/start", "/start@MyBot", "/admin", "/admin@MyBot"])
async def test_explicit_commands_clear_stale_admin_input_and_propagate(command):
    admin, conversation = admin_fake(), ConversationService(MemoryStateStorage())
    await conversation.set_state(
        999, 999, CS.ADMIN_INPUT, action="broadcast", parent=CS.ADMIN_CAT_MSG_PANEL.value
    )
    await reference.handle_message(event(command), admin, conversation)
    assert await conversation.get_state(999, 999) is CS.MAIN
    admin.prepare.assert_not_awaited()
    admin.clear_prompts.assert_awaited_once_with(999)


async def test_start_reply_to_legacy_force_prompt_does_not_become_broadcast_content():
    admin = admin_fake()
    admin.find_prompt = AsyncMock(return_value={"token": "old"})
    actual = event("/start@MyBot", reply_to_msg_id=42)
    await ui.input_reply(actual, admin)
    admin.find_prompt.assert_not_awaited()
    admin.prepare.assert_not_awaited()
    admin.clear_prompts.assert_awaited_once_with(999)


async def test_expected_admin_message_link_is_consumed_before_provider_routing():
    admin, conversation = admin_fake(), ConversationService(MemoryStateStorage())
    await conversation.set_state(
        999,
        999,
        CS.ADMIN_INPUT,
        action="broadcast",
        audience="groups",
        parent=CS.ADMIN_CAT_MSG_PANEL.value,
    )
    with pytest.raises(events.StopPropagation):
        await reference.handle_message(
            event("https://www.instagram.com/reel/abc/"), admin, conversation
        )
    assert admin.prepare.call_args.args[2]["text"].startswith("https://")


async def test_missing_quota_argument_gets_visible_validation_instead_of_dispatch_failure():
    admin, conversation = admin_fake(), ConversationService(MemoryStateStorage())
    await conversation.set_state(
        999, 999, CS.ADMIN_INPUT, action="limit", parent=CS.ADMIN_GROUPS_PANEL.value
    )
    actual = event("-1234")
    with pytest.raises(events.StopPropagation):
        await reference.handle_message(actual, admin, conversation)
    admin.mutate.assert_not_awaited()
    assert "ورودی معتبر نیست" in actual.respond.call_args.args[0]


async def test_revoked_admin_cannot_submit_existing_conversation_content():
    admin, conversation = admin_fake(), ConversationService(MemoryStateStorage())
    admin.authorize.side_effect = DownloadError("denied")
    await conversation.set_state(
        999,
        999,
        CS.ADMIN_INPUT,
        action="broadcast",
        audience="users",
        parent=CS.ADMIN_CAT_MSG_PANEL.value,
    )
    with pytest.raises(events.StopPropagation):
        await reference.handle_message(event("campaign"), admin, conversation)
    admin.prepare.assert_not_awaited()


@pytest.mark.parametrize("value", ["", "x_000", "u_99", "u_999", "g_x00", "g_-10"])
def test_cleanup_codec_rejects_forged_payload(value):
    with pytest.raises(DownloadError):
        decode(value)


def test_cleanup_and_member_callbacks_fit_bigint_owners():
    owner = 2**63 - 1
    for entity, filters in CLEANUP_DEFAULTS.items():
        assert decode(encode(entity, filters)) == (entity, filters)
        assert len(button(owner, "Clean", "cleanprepare", encode(entity, filters)).type.data) <= 64
    assert (
        len(
            button(
                owner, "Members", "mdir", f"{compact_id(-(2**63) + 1)}_1000000_all_yes"
            ).type.data
        )
        <= 64
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("forced_channels", [{"id": 123, "link": "https://t.me/test", "title": "test"}]),
        ("join_points", ["ship"]),
        ("storage_kinds", ["unknown"]),
        ("developer_id", True),
        ("promo", {"enabled": True, "hours": 0, "text": "test"}),
        ("weekly_report", {"enabled": True, "token": "bad"}),
    ],
)
def test_channel_configuration_is_typed_and_allowlisted(key, value):
    with pytest.raises(DownloadError):
        ChannelSettings.validate(key, value)


@pytest.fixture
async def sql_app():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Dedicated PostgreSQL is required")
    namespace = "reference_admin_" + uuid.uuid4().hex
    owner = await asyncpg.connect(dsn)
    await owner.execute(f'CREATE SCHEMA "{namespace}"')
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=12, server_settings={"search_path": namespace}
    )
    bot = uuid.uuid4().int % (2**62)

    def factory():
        return PostgresUnitOfWork(pool, bot)

    await migrate(pool)
    groups = GroupService(GroupRepository(pool, bot), factory)
    admin = AdminService(AdminRepository(pool, bot), factory, groups, 999)
    now = datetime.now(UTC)
    await groups.record(GroupChange(-10, "added", "seed", now, "group", active=True, admin=False))
    app = SimpleNamespace(
        pool=pool,
        bot=bot,
        groups=groups,
        admin=admin,
        factory=factory,
        now=now,
        members=MembershipRepository(pool, bot),
    )
    try:
        yield app
    finally:
        await pool.close()
        await owner.execute(f'DROP SCHEMA "{namespace}" CASCADE')
        await owner.close()


async def seed_member(app, identity=7):
    await app.members.apply(
        MembershipEvent(
            -10, identity, f"m:{identity}", "message", app.now, identity, first_name="Human"
        )
    )


async def test_concurrent_channel_list_edits_are_serialized_without_lost_update(sql_app):
    app = sql_app
    await asyncio.gather(
        *(
            app.admin.channels.upsert_channel(
                999,
                "forced_channels",
                {"id": -1000 - index, "title": str(index), "link": f"https://t.me/test{index}"},
            )
            for index in range(20)
        )
    )
    value = await app.admin.channels.get(999)
    assert len(value["forced_channels"]) == 20
    await app.admin.channels.remove_channel(999, -1001)
    assert len((await app.admin.channels.get())["forced_channels"]) == 19


async def test_watch_repoint_preserves_its_own_type_switches(sql_app):
    app = sql_app
    channel = {"id": -100001, "link": "https://t.me/test", "title": "Test"}
    await app.admin.channels.upsert_watch(
        999, {"chat_id": -10, "channel": channel, "kinds": ["voice"]}
    )
    await app.admin.channels.upsert_watch(
        999, {"chat_id": -10, "channel": {**channel, "id": -100002}, "kinds": ["text"]}
    )
    row = (await app.admin.channels.get())["watches"][0]
    assert row["channel"]["id"] == -100002 and row["kinds"] == ["voice"]


async def test_group_and_member_directory_filters_and_page_clamp(sql_app):
    app = sql_app
    for identity in range(1, 14):
        await seed_member(app, identity)
    await app.pool.execute(
        "UPDATE bot_users SET is_started=TRUE WHERE telegram_account_id=$1 AND user_id=1", app.bot
    )
    await app.pool.execute(
        "UPDATE group_memberships SET is_active=FALSE WHERE telegram_account_id=$1 AND user_id=2",
        app.bot,
    )
    rows, total, page = await app.admin.member_directory(999, -10, 9999)
    assert total == 13 and page == 1 and len(rows) == 3
    rows, total, page = await app.admin.member_directory(999, -10, 0, "out", "no")
    assert total == 1 and rows[0]["user_id"] == 2
    rows, total, page = await app.admin.group_directory(999, 100, True, "members")
    assert total == 1 and page == 0 and rows[0].chat_id == -10


async def test_cancel_clears_both_prompt_and_confirmation_without_other_owner(sql_app):
    app = sql_app
    prompt = await app.admin.repository.request(999, "prompt", "grant", {})
    confirmation = await app.admin.repository.request(999, "confirm", "health", {})
    other = await app.admin.repository.request(998, "confirm", "health", {})
    await app.admin.clear_prompts(999)
    assert await app.admin.repository.consume(999, prompt) is None
    assert await app.admin.repository.consume(999, confirmation) is None
    assert await app.admin.repository.consume(998, other) is not None


async def test_backup_is_strictly_account_scoped_and_excludes_auth_and_pending_work(sql_app):
    app = sql_app
    await seed_member(app)
    result = await app.admin.maintenance.backup(999)
    data = json.loads(result.data)
    assert set(data["tables"]) == set(TABLES)
    assert all(
        row["telegram_account_id"] == app.bot for rows in data["tables"].values() for row in rows
    )
    assert "access_hash" not in result.data.decode()
    token, counts = await app.admin.maintenance.prepare_restore(999, result.data)
    assert counts["group_memberships"] == 1
    await app.admin.maintenance.confirm_restore(999, token)
    with pytest.raises(DownloadError):
        await app.admin.maintenance.confirm_restore(999, token)
    assert (
        await app.pool.fetchval(
            "SELECT sum(message_count) FROM group_memberships WHERE telegram_account_id=$1", app.bot
        )
        == 1
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_account",
        "missing_table",
        "duplicate",
        "invalid_kind",
        "missing_parent",
        "unknown_column",
    ],
)
async def test_restore_rejects_invalid_backup_before_creating_confirmation(sql_app, mutation):
    app = sql_app
    await seed_member(app)
    data = json.loads((await app.admin.maintenance.backup(999)).data)
    if mutation == "foreign_account":
        data["account_id"] += 1
    elif mutation == "missing_table":
        data["tables"].pop("group_events")
    elif mutation == "duplicate":
        data["tables"]["membership_events"] *= 2
    elif mutation == "invalid_kind":
        data["tables"]["membership_events"][0]["kind"] = "sql"
    elif mutation == "missing_parent":
        data["tables"]["bot_users"] = []
    else:
        data["tables"]["bot_groups"][0]["access_hash"] = 123
    with pytest.raises(DownloadError):
        await app.admin.maintenance.prepare_restore(999, json.dumps(data).encode())
    assert (
        await app.pool.fetchval(
            "SELECT count(*) FROM admin_requests WHERE action='restore_public_data'"
        )
        == 0
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "stream",
        "cursor_shape",
        "cursor_string",
        "cursor_bool",
        "cursor_negative",
        "cursor_key",
        "admins_shape",
        "admins_bool",
        "admins_negative",
        "admins_oversized",
        "profile_length",
        "membership_key",
        "lifecycle_key",
    ],
)
async def test_restore_rejects_operationally_malformed_but_json_valid_state(sql_app, mutation):
    app = sql_app
    await seed_member(app)
    data = json.loads((await app.admin.maintenance.backup(999)).data)
    group = data["tables"]["bot_groups"][0]
    if mutation == "stream":
        data["tables"]["group_memberships"][0]["state_event_stream"] = "unknown"
    elif mutation.startswith("cursor"):
        group["event_cursors"] = {
            "cursor_shape": [],
            "cursor_string": {"basic": "3"},
            "cursor_bool": {"basic": True},
            "cursor_negative": {"service": -1},
            "cursor_key": {"s" * 81: 3},
        }[mutation]
    elif mutation.startswith("admins"):
        group["admin_ids"] = {
            "admins_shape": {},
            "admins_bool": [True],
            "admins_negative": [-7],
            "admins_oversized": list(range(1, 202)),
        }[mutation]
    elif mutation == "profile_length":
        data["tables"]["bot_users"][0]["first_name"] = "a" * 257
    elif mutation == "membership_key":
        data["tables"]["membership_events"][0]["event_key"] = "m" * 181
    else:
        data["tables"]["group_events"][0]["event_key"] = "g" * 257
    with pytest.raises(DownloadError):
        await app.admin.maintenance.prepare_restore(999, json.dumps(data).encode())
    assert (
        await app.pool.fetchval(
            "SELECT count(*) FROM admin_requests WHERE action='restore_public_data'"
        )
        == 0
    )


@pytest.mark.parametrize("stream", ["roster", "departure"])
async def test_restore_accepts_new_membership_streams_and_valid_named_group_cursors(
    sql_app, stream
):
    app = sql_app
    await seed_member(app)
    data = json.loads((await app.admin.maintenance.backup(999)).data)
    data["tables"]["group_memberships"][0]["state_event_stream"] = stream
    if stream == "departure":
        data["tables"]["group_memberships"][0]["is_active"] = False
    data["tables"]["bot_groups"][0]["event_cursors"] = {
        "basic": 2,
        "participant": 3,
        "probe": 4,
        "observation": 0,
    }
    data["tables"]["bot_groups"][0]["admin_ids"] = [7, 8]
    await app.pool.execute("DELETE FROM group_memberships WHERE telegram_account_id=$1", app.bot)
    token, _ = await app.admin.maintenance.prepare_restore(999, json.dumps(data).encode())
    await app.admin.maintenance.confirm_restore(999, token)
    assert (
        await app.pool.fetchval(
            "SELECT state_event_stream FROM group_memberships WHERE telegram_account_id=$1", app.bot
        )
        == stream
    )
    await app.members.apply(MembershipEvent(-10, 7, "after-restore", "message", app.now, 1))


async def test_restore_missing_members_preserves_departure_and_revision_fence(sql_app):
    app = sql_app
    await seed_member(app)
    backup = await app.admin.maintenance.backup(999)
    data = json.loads(backup.data)
    if "state_revision" in data["tables"]["group_memberships"][0]:
        data["tables"]["group_memberships"][0]["state_revision"] = 99999999
    await app.pool.execute("DELETE FROM group_memberships WHERE telegram_account_id=$1", app.bot)
    token, _ = await app.admin.maintenance.prepare_restore(999, json.dumps(data).encode())
    await app.admin.maintenance.confirm_restore(999, token)
    row = await app.pool.fetchrow(
        "SELECT state_revision FROM group_memberships WHERE telegram_account_id=$1", app.bot
    )
    assert row["state_revision"] == 0
    await app.pool.execute("DELETE FROM group_memberships WHERE telegram_account_id=$1", app.bot)
    await app.pool.execute(
        "INSERT INTO membership_departures(telegram_account_id,chat_id,user_id,revision,occurred_at,event_key) VALUES($1,-10,7,100,now(),'test:departure')",
        app.bot,
    )
    token, _ = await app.admin.maintenance.prepare_restore(999, backup.data)
    await app.admin.maintenance.confirm_restore(999, token)
    assert (
        await app.pool.fetchval(
            "SELECT count(*) FROM group_memberships WHERE telegram_account_id=$1", app.bot
        )
        == 0
    )


async def test_cleanup_group_deletes_fk_dependents_but_preserves_active_group(sql_app):
    app = sql_app
    await app.groups.record(
        GroupChange(-20, "removed", "gone", app.now, "group", active=False, admin=False)
    )
    await app.pool.execute(
        "INSERT INTO group_request_limits(telegram_account_id,chat_id) VALUES($1,-20)", app.bot
    )
    token, preview = await app.admin.maintenance.prepare_cleanup(
        999, "groups", CLEANUP_DEFAULTS["groups"]
    )
    assert preview["matched"] == 1
    assert await app.admin.maintenance.confirm_cleanup(999, token) == 1
    assert await app.groups.get(-20) is None
    assert await app.groups.get(-10) is not None
    with pytest.raises(DownloadError):
        await app.admin.maintenance.confirm_cleanup(999, token)


async def test_cleanup_retires_only_selected_groups_watches_atomically_and_invalidates_config(
    sql_app,
):
    app = sql_app
    await app.groups.record(
        GroupChange(-20, "removed", "gone", app.now, "group", active=False, admin=False)
    )
    channel = {"id": -1000000000777, "link": "https://t.me/watch", "title": "Watch"}
    await app.admin.channels.upsert_watch(
        999, {"chat_id": -20, "channel": channel, "kinds": ["voice"]}
    )
    await app.admin.channels.upsert_watch(
        999, {"chat_id": -10, "channel": channel, "kinds": ["text"]}
    )
    await app.admin.channels.update(999, "storage", channel)
    assert len((await app.admin.channels.cached())["watches"]) == 2
    token, _ = await app.admin.maintenance.prepare_cleanup(
        999, "groups", CLEANUP_DEFAULTS["groups"]
    )
    assert await app.admin.maintenance.confirm_cleanup(999, token) == 1
    assert app.admin.channels._cached is None and app.admin.channels._expires == 0
    config = await app.admin.channels.cached()
    assert config["watches"] == [{"chat_id": -10, "channel": channel, "kinds": ["text"]}]
    assert config["storage"] == channel


async def test_admin_panel_uses_source_greeting_without_false_saved_confirmation():
    admin, conversation = admin_fake(), ConversationService(MemoryStateStorage())
    admin.channels.get.return_value = {"developer_id": 12345}
    actual = event()
    await reference.panel(actual, admin, conversation)
    assert "ذخیره شد" not in actual.respond.call_args.args[0]
    assert "tg://user?id=12345" not in actual.respond.call_args.args[0]


async def test_start_and_help_render_optional_developer_credit_without_changing_groups():
    from bot.handlers.home import handle_home
    from bot.handlers.start import handle_start

    management = SimpleNamespace(
        developer_credit=AsyncMock(return_value="👨‍💻 توسعه‌دهنده: tg://user?id=12345")
    )
    actual = event(id=5)
    await handle_start(actual, management=management)
    assert "ذخیره شد" not in actual.respond.call_args.args[0]
    assert "tg://user?id=12345" in actual.respond.call_args.args[0]
    actual.pattern_match = (None, b"999", b"help")
    await handle_home(actual, management=management)
    assert "tg://user?id=12345" in actual.edit.call_args.args[0]
    actual.chat_id = -10
    actual.respond.reset_mock()
    management.developer_credit.reset_mock()
    await handle_start(actual, management=management)
    management.developer_credit.assert_not_awaited()
    actual.respond.assert_not_awaited()


async def test_cleanup_never_removes_active_members_or_admins_or_root(sql_app):
    app = sql_app
    for identity in (7, 8, 999):
        await seed_member(app, identity)
    await app.admin.mutate(999, "grant", 8)
    token, preview = (
        None,
        await app.admin.maintenance.cleanup_preview(
            999, "users", {"started": "all", "membership": "all", "kind": "all"}
        ),
    )
    assert preview["matched"] == 0
    await app.pool.execute(
        "UPDATE group_memberships SET is_active=FALSE WHERE telegram_account_id=$1", app.bot
    )
    token, preview = await app.admin.maintenance.prepare_cleanup(
        999, "users", {"started": "all", "membership": "all", "kind": "all"}
    )
    assert preview["matched"] == 1
    assert await app.admin.maintenance.confirm_cleanup(999, token) == 1
    assert await app.admin.user(999, 7) is None
    assert await app.admin.user(999, 8) is not None
    assert await app.admin.user(999, 999) is not None
