import logging
import time
from functools import partial

import httpx
from telethon import events

from bot.keyboards.inline import quality_buttons
from bot.texts.downloads import quality_menu_text
from core.diagnostics import error_fields
from schemas.media import DownloadError

from .urls import LINK_PATTERN, extract_url

log = logging.getLogger("activity")


async def handle_instagram_link(event, service, menus) -> None:
    if event.raw_text.lstrip().startswith("/"):
        return
    url = extract_url(event.raw_text)
    if url is None:
        return
    started = time.monotonic()
    try:
        media = await service.inspect(url)
        token = await menus.create(
            event.sender_id, event.chat_id, media, message_id=getattr(event, "id", None)
        )
        text = quality_menu_text(media)
        for offset in range(0, len(media.qualities), 50):
            chunk = quality_buttons(
                media.qualities[offset : offset + 50], "ig", token, start_index=offset
            )
            await event.respond(
                text,
                buttons=chunk,
                parse_mode=None,
                reply_to=getattr(event, "id", None),
                link_preview=False,
            )
        log.info(
            {
                "event": "quality_menu_shown",
                "site": "instagram",
                "qualities": len(media.qualities),
                "elapsed_seconds": round(time.monotonic() - started, 4),
            }
        )
    except DownloadError as error:
        log.warning(
            {"event": "provider_inspection_failed", "site": "instagram", **error_fields(error)}
        )
        await event.respond(f"⚠️ {error}", parse_mode=None, reply_to=getattr(event, "id", None))
    except (httpx.HTTPError, TimeoutError):
        await event.respond(
            "⚠️ نتونستم اطلاعات این لینک رو بگیرم. کمی بعد دوباره بفرستش.",
            parse_mode=None,
            reply_to=getattr(event, "id", None),
        )


CALLBACK_PREFIX = "ig"


def register_handler(client, service, menus) -> None:
    client.add_event_handler(
        partial(handle_instagram_link, service=service, menus=menus),
        events.NewMessage(incoming=True, pattern=LINK_PATTERN.search),
    )
