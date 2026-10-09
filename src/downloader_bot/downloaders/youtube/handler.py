from functools import partial

import httpx
from telethon import Button, events, types

from downloader_bot.bot.delivery import EXTERNAL_FAILURES
from downloader_bot.schemas.media import DownloadError

from .urls import LINK_PATTERN, extract_url


async def handle_youtube_link(event, service, menus) -> None:
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
        buttons = [
            [Button.inline(quality.label, data=f"yt:{token}:{index}")]
            for index, quality in enumerate(media.qualities)
        ]
        text = f"▶️ YouTube\n\n🎧 {media.title}\n👤 {media.artist}\n\n📥 کیفیت دریافت را انتخاب کن:"
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
        await event.respond(f"⚠️ {error}", parse_mode=None, reply_to=getattr(event, "id", None))
    except (httpx.HTTPError, TimeoutError):
        await event.respond(
            "⚠️ دریافت اطلاعات محتوا ناموفق بود؛ کمی بعد دوباره امتحان کن.",
            parse_mode=None,
            reply_to=getattr(event, "id", None),
        )


CALLBACK_PREFIX = "yt"


def register_handler(client, service, menus) -> None:
    client.add_event_handler(
        partial(handle_youtube_link, service=service, menus=menus),
        events.NewMessage(incoming=True, pattern=LINK_PATTERN.search),
    )
