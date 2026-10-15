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


async def handle_xvideos_link(event, service, menus) -> None:
    if event.raw_text.lstrip().startswith("/"):
        return
    url = extract_url(event.raw_text)
    if url is None:
        return
    started = time.monotonic()
    try:
        media = await service.inspect(url)
        inspected = time.monotonic()
        token = await menus.create(
            event.sender_id, event.chat_id, media, message_id=getattr(event, "id", None)
        )
        buttons = quality_buttons(media.qualities, "xv", token)
        text = quality_menu_text(media)
        stored = time.monotonic()
        await event.respond(
            text,
            buttons=buttons,
            parse_mode=None,
            reply_to=getattr(event, "id", None),
            link_preview=False,
        )
        shown = time.monotonic()
        log.info(
            {
                "event": "quality_menu_shown",
                "site": "xvideos",
                "qualities": len(media.qualities),
                "inspection_seconds": round(inspected - started, 4),
                "menu_storage_seconds": round(stored - inspected, 4),
                "telegram_seconds": round(shown - stored, 4),
                "elapsed_seconds": round(shown - started, 4),
            }
        )
    except DownloadError as error:
        log.warning(
            {"event": "provider_inspection_failed", "site": "xvideos", **error_fields(error)}
        )
        await event.respond(f"⚠️ {error}", parse_mode=None, reply_to=getattr(event, "id", None))
    except (httpx.HTTPError, TimeoutError):
        await event.respond(
            "⚠️ نتونستم اطلاعات این لینک رو بگیرم. کمی بعد دوباره بفرستش.",
            parse_mode=None,
            reply_to=getattr(event, "id", None),
        )


CALLBACK_PREFIX = "xv"


def register_handler(client, service, menus) -> None:
    client.add_event_handler(
        partial(handle_xvideos_link, service=service, menus=menus),
        events.NewMessage(incoming=True, pattern=LINK_PATTERN.search),
    )
