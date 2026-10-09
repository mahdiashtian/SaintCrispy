from functools import partial

import httpx
from telethon import Button, events, types

from downloader_bot.models import DownloadError
from downloader_bot.telegram import EXTERNAL_FAILURES

from .urls import LINK_PATTERN, extract_url


async def handle_xvideos_link(event, service, menus) -> None:
    if event.raw_text.lstrip().startswith("/"):
        return
    url = extract_url(event.raw_text)
    if url is None:
        return
    try:
        media = await service.inspect(url)
        token = menus.add(event.sender_id, event.chat_id, media)
        buttons = [
            [Button.inline(quality.label, data=f"xv:{token}:{index}")]
            for index, quality in enumerate(media.qualities)
        ]
        text = f"{media.title}\n{media.artist}\nکیفیت را انتخاب کن:"
        if media.thumbnail:
            try:
                await event.respond(
                    text,
                    file=types.InputMediaPhotoExternal(media.thumbnail),
                    buttons=buttons,
                    parse_mode=None,
                )
                return
            except EXTERNAL_FAILURES:
                pass
        await event.respond(text, buttons=buttons, parse_mode=None)
    except DownloadError as error:
        await event.respond(str(error), parse_mode=None)
    except (httpx.HTTPError, TimeoutError):
        await event.respond("دریافت اطلاعات محتوا ناموفق بود؛ دوباره امتحان کن.")


CALLBACK_PREFIX = "xv"


def register_handler(client, service, menus) -> None:
    client.add_event_handler(
        partial(handle_xvideos_link, service=service, menus=menus),
        events.NewMessage(incoming=True, pattern=LINK_PATTERN.search),
    )
