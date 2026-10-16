"""Spotify link replies use the original sender's ordinary quality-menu contract."""

import logging
import time
from functools import partial

import httpx
from telethon import events

from bot.inspection import InspectionReply
from bot.keyboards.inline import quality_buttons
from bot.texts.downloads import quality_menu_text
from core.diagnostics import error_fields
from schemas.media import DownloadError

from .urls import LINK_PATTERN, extract_url

log = logging.getLogger("activity")
CALLBACK_PREFIX = "sp"


async def handle_spotify_link(event, service, menus):
    if event.raw_text.lstrip().startswith("/") or (url := extract_url(event.raw_text)) is None:
        return
    started = time.monotonic()
    reply = InspectionReply(event, "spotify")
    try:
        await reply.start()
        media = await service.inspect(url)
        token = await menus.create(
            event.sender_id, event.chat_id, media, message_id=getattr(event, "id", None)
        )
        await reply.show(
            quality_menu_text(media),
            buttons=quality_buttons(media.qualities, CALLBACK_PREFIX, token),
        )
        log.info(
            {
                "event": "quality_menu_shown",
                "site": "spotify",
                "qualities": len(media.qualities),
                "elapsed_seconds": round(time.monotonic() - started, 4),
            }
        )
    except DownloadError as error:
        log.warning(
            {"event": "provider_inspection_failed", "site": "spotify", **error_fields(error)}
        )
        await reply.show(f"⚠️ {error}", buttons=None)
    except (httpx.HTTPError, TimeoutError):
        await reply.show("⚠️ دریافت اطلاعات اسپاتیفای انجام نشد؛ کمی بعد امتحان کن.", buttons=None)
    finally:
        await reply.close()


def register_handler(client, service, menus):
    client.add_event_handler(
        partial(handle_spotify_link, service=service, menus=menus),
        events.NewMessage(incoming=True, pattern=LINK_PATTERN.search),
    )
