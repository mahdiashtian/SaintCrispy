"""Generic Telegram presentation; provider handlers keep their own routing."""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit

from telethon import errors, functions, types

from bot.button_icons import EMOJI_PACK_PREFERENCES, PREMIUM_EMOJI_PACKS
from services.observability import error_fields

log = logging.getLogger("telegram.presentation")


def normalize_pack(value: str) -> str | None:
    value = value.strip()
    if value.startswith(("https://", "http://")):
        parsed = urlsplit(value)
        if parsed.hostname != "t.me" or not parsed.path.startswith("/addemoji/"):
            return None
        value = parsed.path.removeprefix("/addemoji/").rstrip("/")
    return value if re.fullmatch(r"[A-Za-z0-9_]{1,64}", value) else None


def pack_names(configured: str) -> tuple[str, ...]:
    if not configured.strip():
        return ()
    values = [*configured.split(","), *PREMIUM_EMOJI_PACKS, "IconsEmoji"]
    names = {}
    for value in values:
        if name := normalize_pack(value):
            names.setdefault(name.lower(), name)
    return tuple(names.values())


def emoji_key(value: str) -> str:
    return value.replace("\ufe0e", "").replace("\ufe0f", "")


class BotPresentation:
    def __init__(self):
        self.emojis: dict[str, int] = {}
        self.disabled = False
        self.pack_emojis: dict[str, dict[str, int]] = {}
        self.sources: dict[str, str] = {}
        self._pause_until = 0.0
        self._patterns = {}

    @asynccontextmanager
    async def loading(self, client, configured: str):
        task = asyncio.create_task(self.load(client, configured), name="premium-emoji-catalog")
        try:
            yield self
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def load(self, client, short_name: str) -> None:
        names = pack_names(short_name)
        if not names:
            return
        slots = asyncio.Semaphore(3)

        async def fetch(name):
            async with slots:
                if time.monotonic() < self._pause_until:
                    return name, {}
                return name, await self._load_pack(client, name)

        # Failures are isolated per pack; successful results are selected deterministically.
        results = await asyncio.gather(*(fetch(name) for name in names))
        self.pack_emojis = {name: emojis for name, emojis in results if emojis}
        explicit = tuple(
            name
            for item in short_name.split(",")
            if (name := normalize_pack(item)) and name.lower() != "iconsemoji"
        )
        relevant = {
            emoji_key(glyph): preference for glyph, preference in EMOJI_PACK_PREFERENCES.items()
        }
        candidates = {}
        order = {name.lower(): index for index, name in enumerate(names)}
        for name, emojis in self.pack_emojis.items():
            for alt, document_id in emojis.items():
                key = emoji_key(alt)
                if key not in relevant:
                    continue
                priorities = tuple(dict.fromkeys(n.lower() for n in (*explicit, *relevant[key])))
                rank = (
                    priorities.index(name.lower())
                    if name.lower() in priorities
                    else len(priorities) + order[name.lower()]
                )
                candidate = rank, document_id, alt, name
                if key not in candidates or candidate < candidates[key]:
                    candidates[key] = candidate
        selected = {alt: identity for _, identity, alt, _ in candidates.values()}
        self.sources = {alt: name for _, _, alt, name in candidates.values()}
        self.emojis = selected
        self._patterns.clear()
        log.info(
            {
                "event": "emoji_catalog_loaded",
                "requested_packs": len(names),
                "loaded_packs": len(self.pack_emojis),
                "selected_emojis": len(selected),
            }
        )

    async def _load_pack(self, client, short_name: str) -> dict[str, int]:
        try:
            async with asyncio.timeout(4):
                result = await client(
                    functions.messages.GetStickerSetRequest(
                        types.InputStickerSetShortName(short_name), hash=0
                    )
                )
            emojis = {}
            for document in result.documents:
                for attribute in document.attributes:
                    if isinstance(attribute, types.DocumentAttributeCustomEmoji) and attribute.alt:
                        if type(document.id) is int and document.id > 0:
                            emojis.setdefault(attribute.alt, document.id)
            log.info({"event": "emoji_set_loaded", "pack": short_name, "emoji_count": len(emojis)})
            return emojis
        except errors.FloodWaitError as error:
            self._pause_until = max(self._pause_until, time.monotonic() + max(1, error.seconds))
            log.warning({"event": "emoji_catalog_wait", "wait_seconds": max(1, error.seconds)})
        except Exception as error:
            log.warning(
                {"event": "emoji_set_unavailable", "pack": short_name, **error_fields(error)}
            )
        return {}

    def _pattern(self, keys):
        signature = tuple(sorted(keys, key=lambda value: (-len(value), value)))
        if signature not in self._patterns:
            # Do not wrap a component of an unsupported joined/skin-tone emoji.
            alternatives = "|".join(re.escape(emoji) for emoji in signature)
            self._patterns[signature] = re.compile(
                r"(?<!\u200d)(?:"
                + alternatives
                + r")(?![\ufe0e\ufe0f\u200d\u20e3\U0001f3fb-\U0001f3ff\U000e0020-\U000e007f])"
            )
        return self._patterns[signature]

    def prepare(self, text: str) -> tuple[str, list]:
        if self.disabled or not self.emojis:
            return text, []
        aliases = {}
        for alt in self.emojis:
            aliases[alt] = alt
            key = emoji_key(alt)
            aliases[key] = alt
            if len(key) == 1:
                aliases[key + "\ufe0f"] = alt
                aliases[key + "\ufe0e"] = alt
        # Telegram requires the displayed Unicode emoji to match the document's actual alt.
        text = self._pattern(aliases).sub(lambda match: aliases[match.group()], text)
        return text, self.entities(text)

    def entities(self, text: str) -> list:
        if self.disabled or not self.emojis:
            return []
        return [
            types.MessageEntityCustomEmoji(
                offset=len(text[: match.start()].encode("utf-16-le")) // 2,
                length=len(match.group().encode("utf-16-le")) // 2,
                document_id=self.emojis[match.group()],
            )
            for match in list(self._pattern(self.emojis).finditer(text))[:50]
        ]

    async def _send(self, sender, text: str, options):
        if "formatting_entities" in options:
            return await sender(text, **options)
        text, entities = self.prepare(text)
        if not entities:
            return await sender(text, **options)
        try:
            return await sender(text, formatting_entities=entities, **options)
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
            return await sender(text, **options)

    async def respond(self, event, text: str, **options):
        options.setdefault("parse_mode", None)
        options.setdefault("reply_to", getattr(event, "id", None))
        message = await self._send(event.respond, text, options)
        return PresentedMessage(message, self) if hasattr(message, "edit") else message

    async def edit(self, message, text: str, **options):
        options.setdefault("parse_mode", None)
        return await self._send(message.edit, text, options)


class PresentedMessage:
    def __init__(self, message, presentation):
        self.message, self.presentation = message, presentation

    def __getattr__(self, name):
        return getattr(self.message, name)

    async def edit(self, text, **options):
        return await self.presentation.edit(self.message, text, **options)


class PresentedEvent:
    def __init__(self, event, presentation):
        self.event = event
        self.presentation = presentation

    def __getattr__(self, name):
        return getattr(self.event, name)

    async def respond(self, text, **options):
        return await self.presentation.respond(self.event, text, **options)
