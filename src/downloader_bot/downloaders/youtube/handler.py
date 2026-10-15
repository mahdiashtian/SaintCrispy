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


async def handle_youtube_link(event, service, menus) -> None:
    if event.raw_text.lstrip().startswith("/"):
        return
    url = extract_url(event.raw_text)
    if url is None:
        return
    started = time.monotonic()
    reply = InspectionReply(event, "youtube")
    try:
        await reply.start()
        media = await service.inspect(url)
        token = await menus.create(
            event.sender_id, event.chat_id, media, message_id=getattr(event, "id", None)
        )
        buttons = quality_buttons(media.qualities, "yt", token)
        text = quality_menu_text(media)
        # Text buttons need no external-thumbnail fetch before they can be used.
        # The media keeps its artwork for the final file and durable catalog.
        await reply.show(text, buttons=buttons)
        log.info(
            {
                "event": "quality_menu_shown",
                "site": "youtube",
                "qualities": len(media.qualities),
                "elapsed_seconds": round(time.monotonic() - started, 4),
            }
        )
    except DownloadError as error:
        log.warning(
            {"event": "provider_inspection_failed", "site": "youtube", **error_fields(error)}
        )
        await reply.show(f"⚠️ {error}", buttons=None)
    except (httpx.HTTPError, TimeoutError):
        await reply.show(
            "⚠️ نتونستم اطلاعات این لینک رو بگیرم. کمی بعد دوباره بفرستش.",
            buttons=None,
        )
    finally:
        await reply.close()


CALLBACK_PREFIX = "yt"


def register_handler(client, service, menus) -> None:
    callback = partial(handle_youtube_link, service=service, menus=menus)
    callback.provider_site = "youtube"
    client.add_event_handler(
        callback,
        events.NewMessage(incoming=True, pattern=LINK_PATTERN.search),
    )
