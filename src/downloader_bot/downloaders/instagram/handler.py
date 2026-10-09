from functools import partial

import httpx
from telethon import Button, events, types

from downloader_bot.models import DownloadError
from downloader_bot.telegram import EXTERNAL_FAILURES

from .urls import LINK_PATTERN, extract_url


async def handle_instagram_link(event, service, menus) -> None:
    if event.raw_text.lstrip().startswith("/"):
        return
    url = extract_url(event.raw_text)
    if url is None:
        return
    try:
        media = await service.inspect(url)
        token = menus.add(event.sender_id, event.chat_id, media)
        buttons = [
            [Button.inline(quality.label, data=f"ig:{token}:{index}")]
            for index, quality in enumerate(media.qualities)
        ]
        text = f"{media.title}\n{media.artist}\nکیفیت را انتخاب کن:"
        for offset in range(0, len(buttons), 50):
            chunk = buttons[offset : offset + 50]
            if media.thumbnail and offset == 0:
                try:
                    await event.respond(
                        text,
                        file=types.InputMediaPhotoExternal(media.thumbnail),
                        buttons=chunk,
                        parse_mode=None,
                    )
                    continue
                except EXTERNAL_FAILURES:
                    pass
            await event.respond(text, buttons=chunk, parse_mode=None)
    except DownloadError as error:
        await event.respond(str(error), parse_mode=None)
    except (httpx.HTTPError, TimeoutError):
        await event.respond("دریافت اطلاعات محتوا ناموفق بود؛ دوباره امتحان کن.")


CALLBACK_PREFIX = "ig"


def register_handler(client, service, menus) -> None:
    client.add_event_handler(
        partial(handle_instagram_link, service=service, menus=menus),
        events.NewMessage(incoming=True, pattern=LINK_PATTERN.search),
    )
