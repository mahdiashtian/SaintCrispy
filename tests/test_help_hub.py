"""Shared read-only help, private onboarding and source-style topic navigation."""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors

from bot.handlers import admin as admin_ui
from bot.handlers import register_handlers
from bot.handlers.admin_reference import handle_message
from bot.handlers.help import handle_help, handle_help_topic
from bot.handlers.home import handle_home
from bot.handlers.start import handle_start
from bot.premium_emoji import BotPresentation
from bot.texts.guide import TOPICS
from services.access import ProviderAccess


def event(chat=7, raw="/help", data=b"help:7:home", sender=7):
    return SimpleNamespace(
        sender_id=sender,
        chat_id=chat,
        raw_text=raw,
        id=42,
        is_private=chat > 0,
        is_group=chat < 0,
        is_channel=False,
        respond=AsyncMock(),
        edit=AsyncMock(),
        answer=AsyncMock(),
        data=data,
        pattern_match=re.match(rb"(?:help|home):([0-9]+):([a-z_]+)", data),
    )


def buttons(options):
    return [[(item.text, item.type.data) for item in row] for row in options["buttons"]]


@pytest.mark.parametrize("premium", [False, True])
async def test_help_command_in_both_chats_and_private_help_button_are_identical(premium):
    opts = dict(
        bot_username="ActualBot",
        interval=90,
        group_limit=8,
        presentation=BotPresentation() if premium else None,
        management=SimpleNamespace(developer_credit=AsyncMock(return_value="👨‍💻 Developer")),
    )
    private, group = event(), event(-1007)
    await handle_help(private, **opts)
    await handle_help(group, **opts)
    menu = event(data=b"home:7:help")
    await handle_home(menu, **opts)
    first, second, third = private.respond.call_args, group.respond.call_args, menu.edit.call_args
    assert first.args[0] == second.args[0] == third.args[0]
    assert "90" in first.args[0] and "8" in first.args[0] and "@ActualBot" in first.args[0]
    assert buttons(first.kwargs) == buttons(second.kwargs) == buttons(third.kwargs)
    assert [len(row) for row in first.kwargs["buttons"]] == [2, 2, 2]
    assert first.kwargs["reply_to"] == second.kwargs["reply_to"] == 42


@pytest.mark.parametrize("topic,_label", TOPICS)
@pytest.mark.parametrize("chat", [7, -1007])
async def test_topics_edit_one_message_and_return_to_identical_overview(topic, _label, chat):
    actual = event(chat, data=f"help:7:{topic}".encode())
    await handle_help_topic(actual)
    actual.respond.assert_not_awaited()
    assert actual.edit.call_args.args[0]
    assert actual.edit.call_args.kwargs["buttons"][0][0].type.data == b"help:7:home"
    back = event(chat)
    await handle_help_topic(back)
    command = event(chat)
    await handle_help(command)
    assert back.edit.call_args.args[0] == command.respond.call_args.args[0]


@pytest.mark.parametrize("raw", ["/start", "/start@ActualBot", "/start invitation"])
async def test_group_start_neither_replies_nor_changes_users_state_or_settings(raw):
    actual = event(-1007, raw)
    users = SimpleNamespace(start=AsyncMock(), touch_user=AsyncMock())
    conversations = SimpleNamespace(set=AsyncMock())
    management = SimpleNamespace(developer_credit=AsyncMock())
    presentation = SimpleNamespace(respond=AsyncMock())
    await handle_start(
        actual,
        users=users,
        conversations=conversations,
        management=management,
        presentation=presentation,
    )
    actual.respond.assert_not_awaited()
    users.start.assert_not_awaited()
    users.touch_user.assert_not_awaited()
    conversations.set.assert_not_awaited()
    management.developer_credit.assert_not_awaited()
    presentation.respond.assert_not_awaited()


async def test_private_start_still_opens_main_menu_and_registers_subscriber():
    actual = event(raw="/start")
    users = SimpleNamespace(start=AsyncMock(return_value=True))
    await handle_start(actual, users=users)
    users.start.assert_awaited_once_with(7, 7)
    assert actual.respond.call_args.kwargs["buttons"][0][0].type.data == b"home:7:help"


@pytest.mark.parametrize("action", ["main", "sites", "active"])
async def test_old_group_home_buttons_cannot_reopen_private_main_menu(action):
    actual = event(-1007, data=f"home:7:{action}".encode())
    await handle_home(actual)
    assert actual.answer.call_args.kwargs["alert"]
    actual.edit.assert_not_awaited()


async def test_help_topic_is_owned_and_unknown_or_repeated_topic_is_safe():
    other = event(-1007, sender=8)
    await handle_help_topic(other)
    other.edit.assert_not_awaited()
    assert "/help" in other.answer.call_args.args[0] and other.answer.call_args.kwargs["alert"]
    unknown = event(data=b"help:7:missing")
    await handle_help_topic(unknown)
    assert unknown.answer.call_args.kwargs["alert"]
    unknown.edit.assert_not_awaited()
    repeated = event()
    repeated.edit.side_effect = errors.MessageNotModifiedError(None)
    await handle_help_topic(repeated)


async def test_sites_topic_preserves_main_owner_only_youtube_visibility():
    actual = event(data=b"help:7:sites")
    await handle_help_topic(actual, access=ProviderAccess(999))
    assert "YouTube" not in actual.edit.call_args.args[0]
    await handle_help_topic(actual, access=ProviderAccess(7))
    assert "YouTube" in actual.edit.call_args.args[0]


@pytest.mark.parametrize("raw", ["/help", "/help@ActualBot", "راهنما", "📖 راهنما"])
async def test_help_does_not_consume_or_reset_an_administrator_form(raw):
    actual = event(raw=raw)
    actual.reply_to_msg_id = 1
    admin, conversations = SimpleNamespace(), SimpleNamespace()
    # No database/form method should be required to show read-only help.
    await admin_ui.input_reply(actual, admin)
    await handle_message(actual, admin, conversations)
    actual.respond.assert_not_awaited()


async def test_command_for_another_bot_is_ignored():
    actual = event(-1007, raw="/help@OtherBot")
    await handle_help(actual, bot_username="ActualBot")
    actual.respond.assert_not_awaited()


def test_help_is_registered_in_both_chat_types_and_start_policy_is_private_only():
    registrations = []
    client = SimpleNamespace(
        add_event_handler=lambda callback, builder: registrations.append((callback, builder))
    )
    async def check_start(_event):
        pass

    policy = SimpleNamespace(check_start=check_start, recheck=AsyncMock(), observe=AsyncMock())
    register_handlers(client, None, None, policy=policy)
    definitions = {callback.func.__name__: builder for callback, builder in registrations}
    start = definitions["handle_start"]
    assert start.func(event()) and not start.func(event(-1007))
    start_policy = next(
        builder for callback, builder in registrations if callback.func is policy.check_start
    )
    assert start_policy.func(event()) and not start_policy.func(event(-1007))
    for raw in ("/help", "/help@ActualBot", "راهنما", "📖 راهنما"):
        assert definitions["handle_help"].pattern(raw)
    assert definitions["handle_help"].func is None
    assert definitions["handle_help_topic"].match(b"help:7:groups")
