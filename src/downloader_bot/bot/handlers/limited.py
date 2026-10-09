import logging
import secrets

import asyncpg
from telethon import errors

from downloader_bot.bot.presentation import PresentedEvent
from downloader_bot.bot.state.states import ConversationState
from downloader_bot.core.request_context import user_request
from downloader_bot.schemas.media import DownloadError
from downloader_bot.services.observability import error_fields

log = logging.getLogger("activity")


class LimitedHandlers:
    """Wrap provider link handlers without coupling providers to rate-limit storage."""

    def __init__(self, client, limits, conversations=None, users=None, presentation=None):
        self.client = client
        self.limits = limits
        self.conversations = conversations
        self.users = users
        self.presentation = presentation

    def add_event_handler(self, callback, builder) -> None:
        async def limited(event):
            if getattr(event, "raw_text", "").lstrip().startswith("/"):
                return
            if self.presentation:
                event = PresentedEvent(event, self.presentation)
            correlation_id = secrets.token_hex(8)
            with user_request(event.sender_id, event.chat_id, correlation_id):
                try:
                    if self.users:
                        await self.users.touch_user(event.sender_id)
                    await self.limits.check(event.sender_id, "inspect")
                    if self.conversations:
                        await self.conversations.set(
                            event.sender_id,
                            event.chat_id,
                            ConversationState.INSPECTING,
                            request_id=correlation_id,
                        )
                    log.info({"event": "link_received"})
                    try:
                        await callback(event)
                    finally:
                        if self.conversations:
                            await self.conversations.finish_inspection(
                                event.sender_id, event.chat_id, correlation_id
                            )
                except DownloadError as error:
                    log.warning({"event": "link_rejected", **error_fields(error)})
                    await event.respond(f"⚠️ {error}", parse_mode=None)
                    return
                except (asyncpg.PostgresError, OSError, TimeoutError) as error:
                    log.error({"event": "link_storage_failed", **error_fields(error)})
                    await event.respond(
                        "پایگاه داده در دسترس نیست؛ کمی بعد امتحان کن.", parse_mode=None
                    )
                    return
                except errors.RPCError as error:
                    log.warning({"event": "link_reply_failed", **error_fields(error)})
                    # A rejected menu/thumbnail RPC must not escape into other handlers.
                    return
                except Exception as error:
                    log.error({"event": "link_handler_failed", **error_fields(error)})
                    await event.respond(
                        "دریافت اطلاعات به علت خطای داخلی کامل نشد؛ دوباره امتحان کن.",
                        parse_mode=None,
                    )

        self.client.add_event_handler(limited, builder)
