"""Private category menus, native owner-bound controls and live quota configuration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import events, types

from bot.admin_keyboard import reply_event
from bot.handlers import admin as ui
from bot.handlers import admin_reference as panel
from bot.keyboards.admin import button
from bot.state.storage import MemoryStateStorage
from bot.texts import admin_buttons as b
from core.enums import ConversationState as CS
from schemas.media import DownloadError
from services.conversation import ConversationService


def event(raw="", owner=999):
    return SimpleNamespace(
        sender_id=owner,
        chat_id=owner,
        raw_text=raw,
        data=None,
        is_group=False,
        respond=AsyncMock(),
        edit=AsyncMock(),
        answer=AsyncMock(),
    )


def admin():
    return SimpleNamespace(
        authorize=AsyncMock(return_value=True),
        clear_prompts=AsyncMock(),
        options=AsyncMock(
            return_value={
                "root": True,
                "user_links_per_minute": 2,
                "group_links_per_minute": 8,
                "downloads_enabled": True,
                "lifecycle_notifications": True,
            }
        ),
        mutate=AsyncMock(),
        confirm=AsyncMock(return_value=42),
        stats=AsyncMock(return_value={}),
    )


async def send(actual, service, conversations):
    with pytest.raises(events.StopPropagation):
        await panel.handle_message(actual, service, conversations)


async def test_download_settings_enter_change_user_and_group_quotas_and_keep_reply_keyboard():
    conversations, service = ConversationService(MemoryStateStorage()), admin()
    await conversations.set_state(999, 999, CS.ADMIN_PANEL)
    actual = event(b.SETTINGS)
    await send(actual, service, conversations)
    assert await conversations.get_state(999, 999) is CS.ADMIN_DOWNLOAD_SETTINGS
    assert "2" in actual.respond.call_args.args[0] and "8" in actual.respond.call_args.args[0]
    assert isinstance(actual.respond.call_args.kwargs["buttons"], types.ReplyKeyboardMarkup)
    for label, action in ((b.USER_LIMIT, "user_quota"), (b.GROUP_LIMIT, "group_quota")):
        await send(event(label), service, conversations)
        assert (await conversations.get_data(999, 999))["action"] == action
        await send(event("5"), service, conversations)
        service.mutate.assert_awaited_with(999, action, value=5)
        assert await conversations.get_state(999, 999) is CS.ADMIN_DOWNLOAD_SETTINGS


async def test_native_confirmation_survives_restart_and_typed_label_cannot_confirm():
    storage, service = MemoryStateStorage(), admin()
    conversations = ConversationService(storage)
    await conversations.set_state(999, 999, CS.ADMIN_PANEL)
    actual = event()
    adapted = reply_event(actual, conversations)
    await adapted.respond("Confirm", buttons=[[button(999, "✅ تأیید", "confirm", "old-token")]])
    old = actual.respond.call_args.kwargs["buttons"][0][0]
    await adapted.respond("New job", buttons=[[button(999, "✅ تأیید", "confirm", "new-token")]])
    new = actual.respond.call_args.kwargs["buttons"][0][0]
    assert new.type.data != old.type.data
    restarted = ConversationService(storage)
    # Unknown text cannot manufacture an administrator action or consume a token.
    await panel.handle_message(event(old.text), service, restarted)
    await panel.handle_message(event(new.text), service, restarted)
    service.confirm.assert_not_awaited()
    callback = event()
    callback.data = new.type.data
    await ui.callback(callback, service, conversations=restarted)
    service.confirm.assert_awaited_once_with(999, "new-token")


async def test_typed_callback_payload_and_other_owner_keyboard_do_not_grant_admin_access():
    conversations, service = ConversationService(MemoryStateStorage()), admin()
    await conversations.set_state(999, 999, CS.ADMIN_PANEL)
    actual = event()
    await reply_event(actual, conversations).respond(
        "Stats", buttons=[[button(999, "آمار", "stats")]]
    )
    await panel.handle_message(event("adm:999:confirm:token"), service, conversations)
    await panel.handle_message(event("آمار", owner=7), service, conversations)
    service.confirm.assert_not_awaited()
    service.stats.assert_not_awaited()
    service.authorize.side_effect = DownloadError("Revoked")
    callback = event()
    callback.data = b"adm:999:stats:0"
    await ui.callback(callback, service, conversations=conversations)
    service.stats.assert_not_awaited()
    assert callback.answer.call_args.kwargs["alert"]


async def test_inline_admin_callbacks_edit_the_original_message_in_the_registered_runtime():
    actual, service = event(), admin()
    actual.data = b"adm:999:stats:0"
    await ui.callback(actual, service, conversations=ConversationService(MemoryStateStorage()))
    service.stats.assert_awaited_once_with(999)
    actual.edit.assert_awaited_once()
    actual.respond.assert_not_awaited()


async def test_keyboard_navigation_clears_dynamic_actions_without_polluting_input_payload():
    conversations, service = ConversationService(MemoryStateStorage()), admin()
    await conversations.set_state(999, 999, CS.ADMIN_PANEL)
    actual = event()
    await reply_event(actual, conversations).respond(
        "Stats", buttons=[[button(999, "آمار", "stats")]]
    )
    await send(event(b.ADMINS), service, conversations)
    await send(event(b.ADD_ADMIN), service, conversations)
    assert await conversations.get_data(999, 999) == {
        "action": "grant",
        "parent": CS.ADMIN_CAT_ADMINS_PANEL.value,
    }


async def test_help_reads_current_quota_values_instead_of_the_startup_interval():
    from bot.handlers.help import help_view

    values = {"user_limit": 3, "group_limit": 9}
    loader = AsyncMock(side_effect=lambda: dict(values))
    text, _ = await help_view(7, topic="limits", quota_limits=loader)
    assert "3 درخواست" in text and "9 لینک" in text
    values.update(user_limit=5, group_limit=12)
    text, _ = await help_view(7, topic="limits", quota_limits=loader)
    assert "5 درخواست" in text and "12 لینک" in text


@pytest.mark.parametrize(
    "label,action",
    [
        (b.USER_GAP, "user_gap"),
        (b.GROUP_GAP, "group_gap"),
    ],
)
async def test_gap_inputs_keep_parent_menu_and_pass_seconds(label, action):
    conversations, service = ConversationService(MemoryStateStorage()), admin()
    await conversations.set_state(999, 999, CS.ADMIN_DOWNLOAD_SETTINGS)
    await send(event(label), service, conversations)
    assert (await conversations.get_data(999, 999))["action"] == action
    await send(event("120"), service, conversations)
    service.mutate.assert_awaited_with(999, action, value=120)
    assert await conversations.get_state(999, 999) is CS.ADMIN_DOWNLOAD_SETTINGS


@pytest.mark.parametrize(
    "label,action,field,default",
    [
        (b.USER_QUOTA_TOGGLE, "user_quota_toggle", "user_quota_enabled", True),
        (b.GROUP_QUOTA_TOGGLE, "group_quota_toggle", "group_quota_enabled", True),
        (b.USER_GAP_TOGGLE, "user_gap_toggle", "user_gap_enabled", False),
        (b.GROUP_GAP_TOGGLE, "group_gap_toggle", "group_gap_enabled", False),
    ],
)
async def test_each_rate_switch_works_in_both_directions(label, action, field, default):
    conversations, service = ConversationService(MemoryStateStorage()), admin()
    await conversations.set_state(999, 999, CS.ADMIN_DOWNLOAD_SETTINGS)
    for current in (default, not default):
        service.options.return_value[field] = current
        await send(event(label), service, conversations)
        service.mutate.assert_awaited_with(999, action, value=not current)
        assert await conversations.get_state(999, 999) is CS.ADMIN_DOWNLOAD_SETTINGS


async def test_start_and_help_show_disabled_quotas_and_live_gaps():
    from bot.handlers.help import help_view
    from bot.handlers.start import handle_start

    values = {
        "user_limit": 3,
        "group_limit": 9,
        "user_quota_enabled": False,
        "group_quota_enabled": False,
        "user_gap_enabled": True,
        "user_gap_seconds": 17,
        "group_gap_enabled": True,
        "group_gap_seconds": 23,
    }
    loader = AsyncMock(return_value=values)
    for topic in ("home", "limits"):
        text, _ = await help_view(7, topic=topic, quota_limits=loader)
        assert "کاربر خاموش" in text and "گروه خاموش" in text
        assert "17 ثانیه" in text and "23 ثانیه" in text
        assert "3 درخواست" not in text and "9 لینک" not in text
        assert "ادمین‌های ثبت‌شده" in text
    actual = event(owner=7)
    await handle_start(actual, quota_limits=loader)
    text = actual.respond.call_args.args[0]
    assert "کاربر خاموش" in text and "17 ثانیه" in text and "3 درخواست" not in text
