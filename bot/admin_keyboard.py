"""Persist private reply-menu actions; Telegram text never supplies action payloads."""

import secrets
import time

from telethon import types

from bot.keyboards.admin_reply import keyboard
from core.locks import KeyedLocks
from schemas.media import DownloadError

locks = KeyedLocks()
CONFIRMATIONS = {"confirm", "cleanup", "restore", "watchconfirm"}


class AdminReplyEvent:
    def __init__(self, event, conversations):
        self.event, self.conversations = event, conversations

    def __getattr__(self, key):
        return getattr(self.event, key)

    async def respond(self, *args, **options):
        buttons = options.get("buttons")
        if isinstance(buttons, (list, tuple)):
            rows, actions = [], {}
            for row in buttons:
                labels = []
                for button in row:
                    data = button.type.data.decode("ascii")
                    parts = data.split(":")
                    if len(parts) != 4 or parts[:2] != ["adm", str(self.sender_id)]:
                        raise DownloadError("دکمه مدیریت معتبر نیست.")
                    label = button.text
                    if parts[2] in CONFIRMATIONS:
                        # A previous confirmation must not accept a different pending job.
                        label += " · " + secrets.token_hex(3)
                    while label in actions:
                        label += " · " + str(len(actions) + 1)
                    labels.append(label)
                    actions[label] = data
                rows.append(labels)
            await self.conversations.update_data(
                self.sender_id,
                self.chat_id,
                admin_reply_actions=actions,
                admin_reply_expires=time.time() + 1200,
            )
            options["buttons"] = keyboard(rows)
        elif isinstance(buttons, (types.ReplyKeyboardMarkup, types.ReplyKeyboardHide)):
            data = await self.conversations.get_data(self.sender_id, self.chat_id)
            if data.get("admin_reply_actions"):
                await self.conversations.update_data(
                    self.sender_id,
                    self.chat_id,
                    admin_reply_actions={},
                    admin_reply_expires=0,
                )
        return await self.event.respond(*args, **options)

    async def edit(self, *args, **options):
        # Telegram cannot install a reply keyboard by editing an existing message.
        return await self.respond(*args, **options)


def reply_event(event, conversations):
    return event if isinstance(event, AdminReplyEvent) else AdminReplyEvent(event, conversations)
