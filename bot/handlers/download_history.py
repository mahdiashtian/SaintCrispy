"""Owner-scoped download paging; preserve the displayed profile and its entities."""

import copy

from telethon import Button, errors
from telethon.extensions import html

from bot.admin_keyboard import AdminReplyEvent
from bot.handlers.admin_render import source_reply
from bot.texts.couplyo_admin import ADMIN_LOOKUP_AGAIN_HINT, download_history, downloader_user_trace
from schemas.media import DownloadError

HEADER = "ده دریافت اخیر"


def navigation(owner, identity, page):
    index = page.get("download_page", 0)
    row = []
    if index:
        row.append(Button.inline("⬅️", f"dls:{owner}:{identity}:{index - 1}"))
    if page.get("has_next"):
        row.append(Button.inline("➡️", f"dls:{owner}:{identity}:{index + 1}"))
    return [row] if row else []


async def show_trace(event, data, *, presentation=None, hint="", edit=False):
    from bot.handlers.admin_reference import Invocation

    if not data["profile"]:
        raise DownloadError("این کاربر دیگر ثبت نشده است.")
    while isinstance(event, (AdminReplyEvent, Invocation)):
        if isinstance(event, Invocation):
            edit = False
        event = event.event
    markup = downloader_user_trace(data) + hint
    text, _ = html.parse(markup)
    buttons = navigation(event.sender_id, data["profile"]["user_id"], data)
    if len(text.encode("utf-16-le")) // 2 > 4000:
        history = download_history(data)
        await source_reply(
            event, markup[: markup.index(history)], presentation=presentation, edit=edit
        )
        markup = history + hint
        edit = False
    return await source_reply(event, markup, buttons=buttons, presentation=presentation, edit=edit)


async def handle_download_page(event, *, admin, presentation=None):
    from bot.handlers.admin import private

    if not private(event):
        await event.answer("این فهرست فقط در خصوصی در دسترس است.", alert=True)
        return
    try:
        _, owner, identity, page = event.data.decode("ascii").split(":")
        if int(owner) != event.sender_id:
            raise DownloadError("این دکمه متعلق به تو نیست.")
        data = await admin.download_page(event.sender_id, int(identity), int(page))
        if int(page) and not data["downloads"]:
            raise DownloadError("صفحهٔ بعدی وجود ندارد.")
        message = await event.get_message()
        if message is None:
            raise DownloadError("این پیام دیگر در دسترس نیست؛ دوباره ردپای کاربر را باز کن.")
        text = message.raw_text
        start = text.rfind("\n" + HEADER + " 📥")
        if start >= 0:
            start += 1
        elif text.startswith(HEADER + " 📥"):
            start = 0
        if start < 0:
            raise DownloadError("این فهرست منقضی شده؛ دوباره ردپای کاربر را باز کن.")
        end = text.find(ADMIN_LOOKUP_AGAIN_HINT, start)
        if end < 0:
            end = len(text)
        markup = download_history(data).split("\n", 1)[1]
        if presentation:
            markup, _ = presentation.prepare(markup)
        replacement, formatting = html.parse(markup)
        if presentation:
            formatting += presentation.entities(replacement)
        left = len(text[:start].encode("utf-16-le")) // 2
        right = len(text[:end].encode("utf-16-le")) // 2
        delta = len(replacement.encode("utf-16-le")) // 2 - (right - left)
        entities = []
        for entity in message.entities or []:
            item = copy.copy(entity)
            if item.offset + item.length <= left:
                entities.append(item)
            elif item.offset >= right:
                item.offset += delta
                entities.append(item)
        for entity in formatting:
            entity.offset += left
            entities.append(entity)
        options = dict(
            formatting_entities=entities,
            buttons=navigation(event.sender_id, int(identity), data),
            parse_mode=None,
            link_preview=False,
        )
        await event.answer()
        updated = text[:start] + replacement + text[end:]
        if presentation:
            await presentation.edit(event, updated, **options)
        else:
            await event.edit(updated, **options)
    except errors.MessageNotModifiedError:
        pass
    except (DownloadError, ValueError, UnicodeError) as error:
        await event.answer(
            str(error) if isinstance(error, DownloadError) else "دکمه معتبر نیست.", alert=True
        )
