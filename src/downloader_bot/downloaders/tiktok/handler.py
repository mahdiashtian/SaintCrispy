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


async def handle_tiktok_link(event, service, menus) -> None:
    if event.raw_text.lstrip().startswith("/"):
        return
    url = extract_url(event.raw_text)
    if url is None:
        return
    started = time.monotonic()
    reply = InspectionReply(event, "tiktok")
    try:
        await reply.start()
        acknowledged = time.monotonic()
        media = await service.inspect(url)
        inspected = time.monotonic()
        token = await menus.create(
            event.sender_id, event.chat_id, media, message_id=getattr(event, "id", None)
        )
        buttons = quality_buttons(media.qualities, "tt", token)
        text = quality_menu_text(media)
        stored = time.monotonic()
        await reply.show(
            text,
            buttons=buttons,
        )
        shown = time.monotonic()
        log.info(
            {
                "event": "quality_menu_shown",
                "site": "tiktok",
                "qualities": len(media.qualities),
                "acknowledgment_seconds": round(acknowledged - started, 4),
                "inspection_seconds": round(inspected - acknowledged, 4),
                "menu_storage_seconds": round(stored - inspected, 4),
                "telegram_seconds": round(shown - stored, 4),
                "elapsed_seconds": round(shown - started, 4),
            }
        )
    except DownloadError as error:
        log.warning(
            {"event": "provider_inspection_failed", "site": "tiktok", **error_fields(error)}
        )
        await reply.show(f"⚠️ {error}", buttons=None)
    except (httpx.HTTPError, TimeoutError):
        await reply.show(
            "⚠️ نتونستم اطلاعات این لینک رو بگیرم. کمی بعد دوباره بفرستش.",
            buttons=None,
        )
    finally:
        await reply.close()


CALLBACK_PREFIX = "tt"


def register_handler(client, service, menus) -> None:
    client.add_event_handler(
        partial(handle_tiktok_link, service=service, menus=menus),
        events.NewMessage(incoming=True, pattern=LINK_PATTERN.search),
    )
