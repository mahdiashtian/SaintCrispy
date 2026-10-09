from functools import partial

from telethon import events

from downloader_bot.downloaders.instagram.handler import register_handler as register_instagram
from downloader_bot.downloaders.pinterest.handler import register_handler as register_pinterest
from downloader_bot.downloaders.soundcloud.handler import register_handler as register_soundcloud
from downloader_bot.downloaders.xnxx.handler import register_handler as register_xnxx
from downloader_bot.downloaders.xvideos.handler import register_handler as register_xvideos
from downloader_bot.downloaders.youtube.handler import register_handler as register_youtube

from .limited import LimitedHandlers
from .quality import handle_quality
from .start import handle_start
from .stop import handle_stop

PROVIDER_HANDLERS = (
    register_soundcloud,
    register_xnxx,
    register_xvideos,
    register_youtube,
    register_instagram,
    register_pinterest,
)


def register_handlers(
    client,
    service,
    menus,
    jobs=None,
    limits=None,
    *,
    conversations=None,
    users=None,
    bot_username=None,
    presentation=None,
) -> None:
    client.add_event_handler(
        partial(
            handle_start,
            bot_username=bot_username,
            interval=getattr(limits, "interval", 60),
            conversations=conversations,
            users=users,
            presentation=presentation,
        ),
        events.NewMessage(incoming=True, pattern=r"^/start(?:@\w+)?(?:\s.*)?$"),
    )
    provider_client = (
        LimitedHandlers(client, limits, conversations, users, presentation)
        if limits is not None
        else client
    )
    for register in PROVIDER_HANDLERS:
        register(provider_client, service, menus)
    client.add_event_handler(
        partial(
            handle_quality,
            service=service,
            menus=menus,
            jobs=jobs,
            limits=limits,
            conversations=conversations,
            presentation=presentation,
        ),
        events.CallbackQuery(pattern=rb"^(?:sc|xnxx|xv|yt|ig|pin):([0-9a-f]{16}):([0-9]+)$"),
    )
    client.add_event_handler(
        partial(handle_stop, jobs=jobs),
        events.CallbackQuery(pattern=rb"^stop:([0-9a-f]{16})$"),
    )
