"""Preserve native inline controls and canonical private reply-menu labels."""

from telethon import types

from bot.button_icons import button_label
from core.locks import KeyedLocks

locks = KeyedLocks()


class AdminReplyEvent:
    def __init__(self, event, conversations, presentation=None):
        self.event, self.conversations = event, conversations
        self.presentation = presentation

    def __getattr__(self, key):
        return getattr(self.event, key)

    async def respond(self, *args, **options):
        buttons = options.get("buttons")
        updates = {}
        if isinstance(buttons, (types.ReplyKeyboardMarkup, types.ReplyKeyboardHide)):
            data = await self.conversations.get_data(self.sender_id, self.chat_id)
            if data.get("admin_reply_actions"):
                updates.update(
                    admin_reply_actions={},
                    admin_reply_expires=0,
                )
        markup = options.get("buttons")
        if self.presentation is not None and isinstance(
            markup, (types.ReplyKeyboardMarkup, types.ReplyKeyboardHide)
        ):
            aliases = {}
            for row in getattr(markup, "rows", ()):
                for button in row.buttons:
                    label = button_label(button.text)
                    if label != button.text:
                        aliases[label] = button.text
            updates["admin_reply_labels"] = aliases
        if updates:
            await self.conversations.update_data(self.sender_id, self.chat_id, **updates)
        if self.presentation is not None and args:

            async def send(text, **prepared):
                return await self.event.respond(text, *args[1:], **prepared)

            return await self.presentation._send(send, args[0], options)
        return await self.event.respond(*args, **options)

    async def edit(self, *args, **options):
        if getattr(self.event, "data", None) is None or isinstance(
            options.get("buttons"), (types.ReplyKeyboardMarkup, types.ReplyKeyboardHide)
        ):
            return await self.respond(*args, **options)
        if self.presentation is not None and args:
            return await self.presentation._send(self.event.edit, args[0], options)
        return await self.event.edit(*args, **options)


def reply_event(event, conversations, presentation=None):
    if isinstance(event, AdminReplyEvent):
        if presentation is not None:
            event.presentation = presentation
        return event
    return AdminReplyEvent(event, conversations, presentation)
