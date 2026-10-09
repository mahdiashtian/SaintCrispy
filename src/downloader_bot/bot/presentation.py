"""Generic Telegram presentation; provider handlers keep their own routing."""

import asyncio
import logging
import re

from telethon import errors, functions, types

from downloader_bot.services.observability import error_fields

log = logging.getLogger("telegram.presentation")


class BotPresentation:
    def __init__(self):
        self.emojis: dict[str, int] = {}
        self.disabled = False

    async def load(self, client, short_name: str) -> None:
        if not short_name:
            return
        try:
            async with asyncio.timeout(8):
                result = await client(
                    functions.messages.GetStickerSetRequest(
                        types.InputStickerSetShortName(short_name), hash=0
                    )
                )
            for document in result.documents:
                for attribute in document.attributes:
                    if isinstance(attribute, types.DocumentAttributeCustomEmoji) and attribute.alt:
                        self.emojis.setdefault(attribute.alt, document.id)
            log.info({"event": "emoji_set_loaded", "emoji_count": len(self.emojis)})
        except (errors.RPCError, OSError, TimeoutError) as error:
            log.warning({"event": "emoji_set_unavailable", **error_fields(error)})

    def entities(self, text: str) -> list:
        if self.disabled or not self.emojis:
            return []
        pattern = "|".join(re.escape(emoji) for emoji in sorted(self.emojis, key=len, reverse=True))
        return [
            types.MessageEntityCustomEmoji(
                offset=len(text[: match.start()].encode("utf-16-le")) // 2,
                length=len(match.group().encode("utf-16-le")) // 2,
                document_id=self.emojis[match.group()],
            )
            for match in re.finditer(pattern, text)
        ]

    async def respond(self, event, text: str, **options):
        options.setdefault("parse_mode", None)
        options.setdefault("reply_to", getattr(event, "id", None))
        entities = self.entities(text)
        if not entities:
            return await event.respond(text, **options)
        try:
            return await event.respond(text, formatting_entities=entities, **options)
        except errors.RPCError as error:
            if type(error).__name__ not in {
                "CustomEmojiInvalidError",
                "EntityBoundsInvalidError",
                "EntityTypeInvalidError",
                "PremiumAccountRequiredError",
                "EntitiesTooLongError",
                "DocumentInvalidError",
                "EntityMentionUserInvalidError",
            } and not any(
                marker in (getattr(error, "message", "") or "")
                for marker in ("EMOJI", "ENTITY_TYPE_INVALID")
            ):
                raise
            self.disabled = True
            log.warning({"event": "custom_emoji_fallback", **error_fields(error)})
            return await event.respond(text, **options)


class PresentedEvent:
    def __init__(self, event, presentation):
        self.event = event
        self.presentation = presentation

    def __getattr__(self, name):
        return getattr(self.event, name)

    async def respond(self, text, **options):
        return await self.presentation.respond(self.event, text, **options)
