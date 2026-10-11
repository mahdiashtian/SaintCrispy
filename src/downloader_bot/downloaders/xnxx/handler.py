import logging
from functools import partial

import httpx
from telethon import events, types

from downloader_bot.bot.delivery import EXTERNAL_FAILURES
from downloader_bot.bot.presentation import quality_buttons
from downloader_bot.core.diagnostics import error_fields
from downloader_bot.schemas.media import DownloadError

from .urls import LINK_PATTERN, extract_url

log = logging.getLogger("activity")


async def handle_xnxx_link(event, service, menus) -> None:
    if event.raw_text.lstrip().startswith("/"):
        return
    url = extract_url(event.raw_text)
    if url is None:
        return
    try:
        media = await service.inspect(url)
        token = await menus.create(
            event.sender_id, event.chat_id, media, message_id=getattr(event, "id", None)
        )
        buttons = quality_buttons(media.qualities, "xnxx", token)
        text = f"🎬 XNXX\n\n🎧 {media.title}\n👤 {media.artist}\n\nکدوم کیفیت رو می‌خوای؟"
        if media.thumbnail:
            try:
                await event.respond(
                    text,
                    file=types.InputMediaPhotoExternal(media.thumbnail),
                    buttons=buttons,
                    parse_mode=None,
                    reply_to=getattr(event, "id", None),
                )
                return
            except EXTERNAL_FAILURES:
                pass
        await event.respond(
            text, buttons=buttons, parse_mode=None, reply_to=getattr(event, "id", None)
        )
    except DownloadError as error:
        log.warning({"event": "provider_inspection_failed", "site": "xnxx", **error_fields(error)})
        await event.respond(f"⚠️ {error}", parse_mode=None, reply_to=getattr(event, "id", None))
    except (httpx.HTTPError, TimeoutError):
        await event.respond(
            "⚠️ نتونستم اطلاعات این لینک رو بگیرم. کمی بعد دوباره بفرستش.",
            parse_mode=None,
            reply_to=getattr(event, "id", None),
        )


CALLBACK_PREFIX = "xnxx"


def register_handler(client, service, menus) -> None:
    client.add_event_handler(
        partial(handle_xnxx_link, service=service, menus=menus),
        events.NewMessage(incoming=True, pattern=LINK_PATTERN.search),
    )
