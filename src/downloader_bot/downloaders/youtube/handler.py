import asyncio
import logging
import time
from functools import partial

import httpx
from telethon import errors, events

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
    status = None
    inspection = asyncio.create_task(service.inspect(url))

    async def show(text, **options):
        if status is not None and hasattr(status, "edit"):
            try:
                await status.edit(text, parse_mode=None, **options)
                return
            except errors.MessageIdInvalidError:
                # A removed progress message cannot hide the usable quality menu.
                pass
        await event.respond(text, parse_mode=None, reply_to=getattr(event, "id", None), **options)

    try:
        done, _ = await asyncio.wait({inspection}, timeout=0.15)
        if not done:
            try:
                async with asyncio.timeout(1):
                    status = await event.respond(
                        "▶️ لینک یوتیوب رو گرفتم؛ دارم کیفیت‌های قابل دانلود رو بررسی می‌کنم…",
                        parse_mode=None,
                        reply_to=getattr(event, "id", None),
                    )
                log.info(
                    {
                        "event": "youtube_link_acknowledged",
                        "elapsed_seconds": round(time.monotonic() - started, 4),
                    }
                )
            except (TimeoutError, OSError):
                # A slow acknowledgment must not cancel the useful inspection.
                pass
        media = await inspection
        token = await menus.create(
            event.sender_id, event.chat_id, media, message_id=getattr(event, "id", None)
        )
        buttons = quality_buttons(media.qualities, "yt", token)
        text = quality_menu_text(media)
        # Text buttons need no external-thumbnail fetch before they can be used.
        # The media keeps its artwork for the final file and durable catalog.
        await show(text, buttons=buttons, link_preview=False)
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
        await show(f"⚠️ {error}", buttons=None, link_preview=False)
    except (httpx.HTTPError, TimeoutError):
        await show(
            "⚠️ نتونستم اطلاعات این لینک رو بگیرم. کمی بعد دوباره بفرستش.",
            buttons=None,
            link_preview=False,
        )
    finally:
        if not inspection.done():
            inspection.cancel()
        await asyncio.gather(inspection, return_exceptions=True)


CALLBACK_PREFIX = "yt"


def register_handler(client, service, menus) -> None:
    client.add_event_handler(
        partial(handle_youtube_link, service=service, menus=menus),
        events.NewMessage(incoming=True, pattern=LINK_PATTERN.search),
    )
