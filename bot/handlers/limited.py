import asyncio
import logging
import secrets
import time
from collections import OrderedDict

import asyncpg
from telethon import errors

from bot.premium_emoji import PresentedEvent
from bot.state.states import ConversationState
from core.request_context import inspection_work, user_request
from schemas.media import DownloadError
from services.access import ProviderAccess
from services.observability import error_fields

log = logging.getLogger("activity")


class LimitedHandlers:
    """Wrap provider link handlers without coupling providers to rate-limit storage."""

    def __init__(
        self,
        client,
        limits,
        conversations=None,
        users=None,
        presentation=None,
        *,
        max_active=1000,
        access=None,
    ):
        if max_active < 1:
            raise ValueError("Link handler capacity must be positive")
        self.client = client
        self.limits = limits
        self.conversations = conversations
        self.users = users
        self.presentation = presentation
        self.max_active = max_active
        self.access = access or ProviderAccess()
        self.active = 0
        self.peak_active = 0
        self._error_replies = 0
        self._warnings: OrderedDict[tuple, float] = OrderedDict()

    def _count(self, name):
        if telemetry := getattr(self.limits, "telemetry", None):
            telemetry.counters[name] += 1

    async def _respond(self, event, text, *, reason=None):
        # Rejected requests must not create an unbounded backlog of Telegram RPCs.
        now = time.monotonic()
        while self._warnings and next(iter(self._warnings.values())) <= now:
            self._warnings.popitem(last=False)
        owner = None if event.chat_id < 0 and reason == "group_rate_limited" else event.sender_id
        key = owner, event.chat_id, reason
        if self._error_replies >= 32 or (reason and self._warnings.get(key, 0) > now):
            self._count("link_error_replies_suppressed")
            return
        if reason:
            self._warnings[key] = now + 5
            self._warnings.move_to_end(key)
            if len(self._warnings) > 2048:
                self._warnings.popitem(last=False)
        self._error_replies += 1
        try:
            async with asyncio.timeout(5):
                await event.respond(text, parse_mode=None, reply_to=getattr(event, "id", None))
        except Exception as error:
            self._count("link_error_reply_failures")
            log.warning({"event": "link_error_reply_failed", **error_fields(error)})
        finally:
            self._error_replies -= 1

    def add_event_handler(self, callback, builder) -> None:
        async def execute(event):
            correlation_id = secrets.token_hex(8)
            with user_request(event.sender_id, event.chat_id, correlation_id):
                try:
                    combined = False
                    if check_link := getattr(self.limits, "check_link", None):
                        combined = await check_link(event.sender_id, event.chat_id, correlation_id)
                    elif self.limits is not None:
                        await self.limits.check(event.sender_id, "inspect")
                    if self.users and not combined:
                        await self.users.touch_user(event.sender_id)
                    if self.conversations and not combined:
                        await self.conversations.set(
                            event.sender_id,
                            event.chat_id,
                            ConversationState.INSPECTING,
                            request_id=correlation_id,
                        )
                    elif combined:
                        log.info({"event": "conversation_changed", "state": "INSPECTING"})
                    log.info({"event": "link_received"})
                    with inspection_work(combined and self.conversations is not None) as work:
                        try:
                            await callback(event)
                        finally:
                            if self.conversations and not (work and work.completed):
                                await self.conversations.finish_inspection(
                                    event.sender_id, event.chat_id, correlation_id
                                )
                except DownloadError as error:
                    log.warning({"event": "link_rejected", **error_fields(error)})
                    await self._respond(event, f"⚠️ {error}", reason=error.code)
                    return
                except (asyncpg.PostgresError, OSError, TimeoutError) as error:
                    log.error({"event": "link_storage_failed", **error_fields(error)})
                    await self._respond(event, "پایگاه داده در دسترس نیست؛ کمی بعد امتحان کن.")
                    return
                except errors.RPCError as error:
                    log.warning({"event": "link_reply_failed", **error_fields(error)})
                    # A rejected menu/thumbnail RPC must not escape into other handlers.
                    return
                except Exception as error:
                    log.error({"event": "link_handler_failed", **error_fields(error)})
                    await self._respond(
                        event,
                        "دریافت اطلاعات به علت خطای داخلی کامل نشد؛ دوباره امتحان کن.",
                    )

        async def limited(event):
            if getattr(event, "raw_text", "").lstrip().startswith("/"):
                return
            if self.presentation:
                event = PresentedEvent(event, self.presentation)
            try:
                self.access.require(getattr(callback, "provider_site", None), event.sender_id)
            except DownloadError as error:
                with user_request(event.sender_id, event.chat_id):
                    log.info({"event": "provider_access_denied", "site": "youtube"})
                await self._respond(event, f"⛔️ {error}", reason=error.code)
                return
            if self.active >= self.max_active:
                self._count("link_handler_capacity_rejections")
                await self._respond(
                    event, "⏳ ظرفیت بررسی لینک‌ها پر است؛ کمی بعد دوباره بفرست.", reason="capacity"
                )
                return
            # Reserve before the first await, including SQL admission and pool waits.
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            try:
                await execute(event)
            finally:
                self.active -= 1

        limited.func = getattr(callback, "func", callback)
        self.client.add_event_handler(limited, builder)
