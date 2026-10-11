import logging

from telethon import Button

from downloader_bot.bot.state.states import ConversationState
from downloader_bot.bot.texts import start_text
from downloader_bot.core.request_context import user_request

log = logging.getLogger("activity")


def start_buttons(owner_id: int):
    return [
        [
            Button.inline("راهنما", f"home:{owner_id}:help"),
            Button.inline("سایت‌ها", f"home:{owner_id}:sites"),
        ],
        [Button.inline("درخواست‌های من", f"home:{owner_id}:active")],
    ]


async def handle_start(
    event, bot_username=None, interval=60, conversations=None, users=None, presentation=None
) -> None:
    with user_request(event.sender_id, event.chat_id):
        if users:
            await users.touch_user(event.sender_id, started=True)
        if conversations:
            await conversations.set(event.sender_id, event.chat_id, ConversationState.MAIN)
        text = start_text(bot_username, interval)
        buttons = start_buttons(event.sender_id)
        log.info({"event": "bot_started_by_user"})
        if presentation:
            await presentation.respond(event, text, buttons=buttons)
        else:
            await event.respond(
                text, buttons=buttons, parse_mode=None, reply_to=getattr(event, "id", None)
            )
