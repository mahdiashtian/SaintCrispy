"""One help hub for private menus, group commands and topic callbacks."""

from telethon import errors

from bot.deeplinks import bot_handle, normalize_bot_username
from bot.keyboards.inline import help_buttons
from bot.texts import help_text
from bot.texts.guide import topic_text


async def help_view(
    owner_id,
    *,
    topic="home",
    bot_username=None,
    interval=60,
    group_limit=5,
    access=None,
    management=None,
    quota_limits=None,
):
    limits = await quota_limits() if quota_limits is not None else None
    user_limit = limits["user_limit"] if limits else None
    group_limit = limits["group_limit"] if limits else group_limit
    text = (
        help_text(interval, group_limit, user_limit=user_limit)
        + f"\n\n💬 ربات: {bot_handle(bot_username)}"
        if topic == "home"
        else topic_text(
            topic,
            interval=interval,
            group_limit=group_limit,
            youtube_enabled=bool(access and access.allows("youtube", owner_id)),
            user_limit=user_limit,
        )
    )
    if text is None:
        return None
    if management is not None:
        credit = await management.developer_credit()
        if credit:
            text += "\n\n" + credit
    return text, help_buttons(owner_id, topic=topic)


async def handle_help(event, *, presentation=None, **options):
    raw = (getattr(event, "raw_text", "") or "").strip()
    username = normalize_bot_username(options.get("bot_username"))
    if raw.startswith("/help@") and username:
        if raw.split("@", 1)[1].lower() != username.lower():
            return
    text, buttons = await help_view(event.sender_id, **options)
    if presentation:
        await presentation.respond(event, text, buttons=buttons)
    else:
        await event.respond(
            text, buttons=buttons, parse_mode=None, reply_to=getattr(event, "id", None)
        )


async def handle_help_topic(event, *, presentation=None, **options):
    owner_id = int(event.pattern_match[1])
    if owner_id != event.sender_id:
        await event.answer(
            "این راهنما برای تو نیست؛ /help رو بفرست تا راهنمای خودت باز بشه.", alert=True
        )
        return
    view = await help_view(owner_id, topic=event.pattern_match[2].decode("ascii"), **options)
    if view is None:
        await event.answer("موضوع پیدا نشد 🤔", alert=True)
        return
    text, buttons = view
    await event.answer()
    try:
        if presentation:
            await presentation.edit(event, text, buttons=buttons)
        else:
            await event.edit(text, buttons=buttons, parse_mode=None)
    except errors.MessageNotModifiedError:
        pass
