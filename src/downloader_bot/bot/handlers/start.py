from downloader_bot.bot.state.states import ConversationState
from downloader_bot.bot.texts import start_text
from downloader_bot.core.request_context import user_request


async def handle_start(
    event, bot_username=None, interval=60, conversations=None, users=None, presentation=None
) -> None:
    with user_request(event.sender_id, event.chat_id):
        if users:
            await users.touch_user(event.sender_id, started=True)
        if conversations:
            await conversations.set(event.sender_id, event.chat_id, ConversationState.MAIN)
        text = start_text(bot_username, interval)
        if presentation:
            await presentation.respond(event, text)
        else:
            await event.respond(text, parse_mode=None, reply_to=getattr(event, "id", None))
