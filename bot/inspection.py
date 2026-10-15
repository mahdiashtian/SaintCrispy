"""Telegram acknowledgment and menu editing; no provider extraction logic."""

import asyncio
import logging

from telethon import errors

from bot.texts.downloads import inspection_text
from core.diagnostics import error_fields

log = logging.getLogger("activity")


class InspectionReply:
    def __init__(self, event, site):
        self.event = event
        self.site = site
        self.message = None
        self.shown = False

    async def start(self):
        try:
            async with asyncio.timeout(5):
                self.message = await self._respond(inspection_text(self.site))
        except (OSError, TimeoutError) as error:
            # An unavailable acknowledgment must not cancel useful inspection.
            log.warning(
                {"event": "inspection_ack_failed", "site": self.site, **error_fields(error)}
            )

    async def _respond(self, text, **options):
        return await self.event.respond(
            text,
            parse_mode=None,
            reply_to=getattr(self.event, "id", None),
            link_preview=False,
            **options,
        )

    async def show(self, text, **options):
        if not self.shown and self.message is not None:
            try:
                await self.message.edit(text, parse_mode=None, link_preview=False, **options)
            except errors.MessageNotModifiedError:
                pass
            except errors.MessageIdInvalidError:
                # A deleted acknowledgment cannot hide the usable menu/error.
                await self._respond(text, **options)
        else:
            # Albums may need more than Telegram's 50 buttons per message.
            await self._respond(text, **options)
        self.shown = True

    async def close(self):
        if self.shown or self.message is None:
            return
        try:
            async with asyncio.timeout(3):
                await self.message.delete()
        except (errors.RPCError, OSError, TimeoutError) as error:
            log.warning(
                {"event": "inspection_ack_cleanup_failed", "site": self.site, **error_fields(error)}
            )
