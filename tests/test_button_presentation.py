"""Single button icons preserve Telegram actions and private reply-menu routing."""

from unittest.mock import AsyncMock

import pytest
from telethon import Button, errors, events, types
from test_admin_reference_database import app as app
from test_download_settings import admin, event

from bot.admin_keyboard import reply_event
from bot.button_icons import BUTTON_EMOJI, button_label
from bot.handlers import admin as admin_ui
from bot.handlers import admin_reference as panel
from bot.keyboards import admin_reply
from bot.keyboards.admin import button
from bot.keyboards.inline import help_buttons, start_buttons
from bot.premium_emoji import BotPresentation
from bot.state.storage import MemoryStateStorage
from bot.texts import admin_buttons as labels
from core.enums import ConversationState as CS
from repositories.postgres.workflow import WorkflowRepository
from services.conversation import ConversationService


def presentation():
    result = BotPresentation()
    result.button_emojis = {"⚙️": 101, "📥": 102, "✅": 103, "⬜": 104}
    return result


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("📥 دریافت", "دریافت"),
        ("توقف ⏹", "توقف"),
        ("✅ 💬 پیام عادی", "پیام عادی"),
        ("👨‍💻 توسعه‌دهنده", "توسعه‌دهنده"),
        ("👍🏽 تأیید", "تأیید"),
        ("🇮🇷 فارسی", "فارسی"),
        ("1️⃣ گزینه 1", "گزینه 1"),
        ("▶︎ پخش", "پخش"),
        ("MP3 · 128 kbps", "MP3 · 128 kbps"),
        ("1080p · 60 fps", "1080p · 60 fps"),
        ("⬅️", "بازگشت"),
    ],
)
def test_labels_remove_complete_decorations_without_changing_quality_values(original, expected):
    assert button_label(original) == expected


def test_all_static_admin_and_public_buttons_have_one_icon_and_keep_original_actions():
    renderer = presentation()
    keyboards = [admin_reply.menu(state) for state in admin_reply.MENUS]
    keyboards += [start_buttons(7), help_buttons(7), help_buttons(7, topic="commands")]
    keyboards += [admin_reply.toggles(labels.KINDS, {"video", "text"})]
    for keyboard in keyboards:
        rendered = renderer.buttons(keyboard)
        before = (
            [item for row in keyboard.rows for item in row.buttons]
            if hasattr(keyboard, "rows")
            else [item for row in keyboard for item in row]
        )
        after = (
            [item for row in rendered.rows for item in row.buttons]
            if hasattr(rendered, "rows")
            else [item for row in rendered for item in row]
        )
        for original, actual in zip(before, after, strict=True):
            assert actual.text == button_label(original.text)
            assert actual.style.icon
            assert not BUTTON_EMOJI.search(actual.text)
            assert actual.type.to_dict() == original.type.to_dict()
            bytes(actual)


@pytest.mark.parametrize("text,label", [("⬅️", "بازگشت"), ("➡️", "بعدی"), ("✅", "تأیید")])
def test_navigation_uses_readable_text_and_one_premium_icon(text, label):
    original = Button.inline(text, b"same", icon=999)
    rendered = presentation().buttons([[original]])[0][0]
    assert rendered.text == label and rendered.style.icon == 999
    assert rendered.type.data == b"same"
    bytes(rendered)


def test_toggle_status_owns_the_single_icon_instead_of_the_message_kind_decoration():
    renderer = presentation()
    renderer.button_emojis = {"🎬": 1, "⬜": 2, "✅": 3}
    keyboard = admin_reply.toggles(labels.KINDS, {"video"})
    rendered = renderer.buttons(keyboard)
    video = next(item for row in rendered.rows for item in row.buttons if item.text == "فیلم")
    photo = next(item for row in rendered.rows for item in row.buttons if item.text == "عکس")
    assert video.style.icon == 3 and photo.style.icon == 2


@pytest.mark.parametrize("disabled", [False, True])
def test_no_catalog_or_disabled_premium_keeps_one_unicode_icon(disabled):
    renderer = BotPresentation()
    renderer.disabled = disabled
    original = Button.inline("📥 دریافت", b"same")
    rendered = renderer.buttons([[original]])[0][0]
    assert rendered.text == original.text and rendered.style.icon is None


async def test_premium_rejection_restores_original_labels_and_preserves_callback_and_color():
    original = Button.inline("📥 دریافت", b"same", style="danger")
    renderer = presentation()
    sender = AsyncMock(side_effect=[errors.PremiumAccountRequiredError(None), None])
    await renderer._send(sender, "متن", {"buttons": [[original]]})
    first, fallback = [call.kwargs["buttons"][0][0] for call in sender.await_args_list]
    assert first.text == "دریافت" and first.style.icon == 102
    assert fallback.text == "📥 دریافت" and fallback.style.icon is None
    assert first.type.data == fallback.type.data == original.type.data
    assert first.style.bg_danger and fallback.style.bg_danger
    assert original.text == "📥 دریافت" and original.style.icon is None


async def dispatch(actual, service, conversations, renderer):
    with pytest.raises(events.StopPropagation):
        await panel.handle_message(actual, service, conversations, renderer)


async def test_premium_admin_reply_labels_route_categories_back_input_and_cancel_after_restart():
    storage = MemoryStateStorage()
    conversations, service, renderer = ConversationService(storage), admin(), presentation()
    await panel.panel(reply_event(event(), conversations, renderer), service, conversations)
    assert "تنظیمات دانلود" in (await conversations.get_data(999, 999))["admin_reply_labels"]
    conversations = ConversationService(storage)
    await dispatch(event("تنظیمات دانلود"), service, conversations, renderer)
    assert await conversations.get_state(999, 999) is CS.ADMIN_DOWNLOAD_SETTINGS
    await dispatch(event("محدودیت کاربر در دقیقه"), service, conversations, renderer)
    assert (await conversations.get_data(999, 999))["action"] == "user_quota"
    await dispatch(event("5"), service, conversations, renderer)
    service.mutate.assert_awaited_with(999, "user_quota", value=5)
    await dispatch(event("بازگشت"), service, conversations, renderer)
    assert await conversations.get_state(999, 999) is CS.ADMIN_PANEL
    await dispatch(event("انصراف"), service, conversations, renderer)
    assert await conversations.get_state(999, 999) is CS.MAIN


@pytest.mark.parametrize("reject_icons", [False, True])
async def test_dynamic_premium_confirmation_keeps_native_owner_and_token_payloads(
    reject_icons,
):
    storage = MemoryStateStorage()
    conversations, service, renderer = ConversationService(storage), admin(), presentation()
    await conversations.set_state(999, 999, CS.ADMIN_PANEL)
    actual = event()
    if reject_icons:
        actual.respond.side_effect = [errors.PremiumAccountRequiredError(None), None, None]
    adapted = reply_event(actual, conversations, renderer)
    await adapted.respond("Confirm", buttons=[[button(999, "✅ تأیید", "confirm", "old")]])
    old = actual.respond.call_args.kwargs["buttons"][0][0]
    await adapted.respond("Confirm", buttons=[[button(999, "✅ تأیید", "confirm", "new")]])
    new = actual.respond.call_args.kwargs["buttons"][0][0]
    assert old.type.data != new.type.data
    conversations = ConversationService(storage)
    await panel.handle_message(event(old.text), service, conversations, renderer)
    foreign = event(owner=7)
    foreign.data = new.type.data
    await admin_ui.callback(foreign, service, renderer, conversations)
    service.confirm.assert_not_awaited()
    actual = event()
    actual.data = new.type.data
    await admin_ui.callback(actual, service, renderer, conversations)
    service.confirm.assert_awaited_once_with(999, "new")


async def test_dynamic_actions_remain_distinct_when_their_plain_labels_match():
    renderer = presentation()
    conversations = ConversationService(MemoryStateStorage())
    actual = event()
    await reply_event(actual, conversations, renderer).respond(
        "Choose",
        buttons=[[button(999, "📥 مورد", "stats"), button(999, "✅ مورد", "daily")]],
    )
    buttons = actual.respond.call_args.kwargs["buttons"][0]
    assert len({item.text for item in buttons}) == 1
    assert len({item.type.data for item in buttons}) == 2
    saved = await conversations.get_data(999, 999)
    assert "admin_reply_actions" not in saved


async def test_caller_supplied_icon_is_retained_without_its_unicode_duplicate():
    renderer = presentation()
    result = renderer.buttons([[Button.inline("👨‍💻 توسعه‌دهنده", b"same", icon=999)]])[0][0]
    assert result.text == "توسعه‌دهنده" and result.style.icon == 999


async def test_reply_menu_hide_clears_visible_label_aliases():
    conversations = ConversationService(MemoryStateStorage())
    actual = event()
    adapted = reply_event(actual, conversations, presentation())
    await adapted.respond("Menu", buttons=admin_reply.menu(CS.ADMIN_PANEL))
    assert (await conversations.get_data(999, 999))["admin_reply_labels"]
    await adapted.respond("Done", buttons=types.ReplyKeyboardHide())
    assert (await conversations.get_data(999, 999))["admin_reply_labels"] == {}


async def test_visible_reply_labels_survive_a_fresh_sql_repository_and_keep_owner_scope(app):
    conversations = ConversationService(app.workflow)
    renderer = presentation()
    await panel.panel(reply_event(event(), conversations, renderer), app.admin, conversations)
    restarted = ConversationService(WorkflowRepository(app.pool, app.bot))
    await dispatch(event("مدیریت گروه‌ها"), app.admin, restarted, renderer)
    assert await restarted.get_state(999, 999) is CS.ADMIN_GROUPS_PANEL
    assert await restarted.get_data(7, 7) == {}
    await dispatch(event("بازگشت"), app.admin, restarted, renderer)
    assert await restarted.get_state(999, 999) is CS.ADMIN_PANEL
