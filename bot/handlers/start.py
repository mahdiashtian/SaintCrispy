import logging

from bot.keyboards.inline import start_buttons as start_buttons
from bot.state.states import ConversationState
from bot.texts import start_text
from bot.texts.messages import group_start_text
from core.request_context import user_request

log = logging.getLogger("activity")


async def handle_start(
    event,
    bot_username=None,
    interval=60,
    conversations=None,
    users=None,
    presentation=None,
    group_limit=5,
) -> None:
    in_group = event.chat_id < 0
    with user_request(event.sender_id, event.chat_id):
        combined = False
        if users:
            if (start := getattr(users, "start", None)) and not in_group:
                combined = await start(event.sender_id, event.chat_id)
            else:
                await users.touch_user(event.sender_id, started=not in_group)
        if conversations and not combined:
            await conversations.set(event.sender_id, event.chat_id, ConversationState.MAIN)
        text = start_text(bot_username, interval)
        if in_group:
            text = group_start_text(bot_username, interval, group_limit)
        buttons = start_buttons(event.sender_id)
        log.info({"event": "bot_started_by_user"})
        if presentation:
            await presentation.respond(event, text, buttons=buttons)
        else:
            await event.respond(
                text, buttons=buttons, parse_mode=None, reply_to=getattr(event, "id", None)
            )
