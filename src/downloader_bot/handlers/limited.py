import asyncpg
from telethon import errors

from downloader_bot.models import DownloadError
from downloader_bot.request_context import user_request


class LimitedHandlers:
    """Wrap provider link handlers without coupling providers to rate-limit storage."""

    def __init__(self, client, limits):
        self.client = client
        self.limits = limits

    def add_event_handler(self, callback, builder) -> None:
        async def limited(event):
            if getattr(event, "raw_text", "").lstrip().startswith("/"):
                return
            try:
                await self.limits.check(event.sender_id, "inspect")
                with user_request(event.sender_id):
                    await callback(event)
            except DownloadError as error:
                await event.respond(str(error), parse_mode=None)
                return
            except (asyncpg.PostgresError, OSError, TimeoutError):
                await event.respond(
                    "پایگاه داده در دسترس نیست؛ کمی بعد امتحان کن.", parse_mode=None
                )
                return
            except errors.RPCError:
                # A rejected menu/thumbnail RPC must not escape into other handlers.
                return
            except Exception:
                await event.respond(
                    "دریافت اطلاعات به علت خطای داخلی کامل نشد؛ دوباره امتحان کن.", parse_mode=None
                )

        self.client.add_event_handler(limited, builder)
