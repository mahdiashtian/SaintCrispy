"""Generic Telegram presentation; provider handlers keep their own routing."""

import asyncio
import copy
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit

from telethon import errors, functions, types

from bot.button_icons import (
    BUTTON_EMOJI,
    EMOJI_FALLBACKS,
    EMOJI_PACK_PREFERENCES,
    PREMIUM_EMOJI_PACKS,
    button_label,
)
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


def semantic_aliases(icons):
    available = {emoji_key(alt): alt for alt in icons}
    resolved = {}
    for source, candidates in EMOJI_FALLBACKS.items():
        if emoji_key(source) in available:
            continue
        target = next(
            (
                available[emoji_key(candidate)]
                for candidate in candidates
                if emoji_key(candidate) in available
            ),
            None,
        )
        if target is not None:
            resolved[source] = target
    return resolved


class BotPresentation:
    def __init__(self):
        self.emojis: dict[str, int] = {}
        self.button_emojis: dict[str, int] = {}
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
                priorities = tuple(
                    dict.fromkeys(n.lower() for n in (*explicit, *relevant.get(key, ())))
                )
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
        self.button_emojis = selected
        self.emojis = {
            alt: identity for alt, identity in selected.items() if emoji_key(alt) in relevant
        }
        self.sources = {alt: name for alt, name in self.sources.items() if alt in self.emojis}
        self._patterns.clear()
        log.info(
            {
                "event": "emoji_catalog_loaded",
                "requested_packs": len(names),
                "loaded_packs": len(self.pack_emojis),
                "selected_emojis": len(selected),
                "body_emojis": len(self.emojis),
                "semantic_aliases": len(semantic_aliases(self.emojis)),
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
        for source, target in {
            **{alt: alt for alt in self.emojis},
            **semantic_aliases(self.emojis),
        }.items():
            aliases[source] = target
            key = emoji_key(source)
            aliases[key] = target
            if len(key) == 1:
                aliases[key + "\ufe0f"] = target
                aliases[key + "\ufe0e"] = target
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

    def buttons(self, buttons, *, plain=False):
        """Use one icon per button while preserving actions and existing colors."""
        buttons = copy.deepcopy(buttons)
        icons = {} if plain or self.disabled else {**self.button_emojis, **self.emojis}
        aliases = semantic_aliases(icons)
        available = {emoji_key(alt): alt for alt in icons}
        fallbacks = {emoji_key(source): target for source, target in aliases.items()}

        def decorate(button):
            if isinstance(button, (list, tuple)):
                for item in button:
                    decorate(item)
            elif hasattr(button, "rows"):
                for row in button.rows:
                    decorate(row.buttons)
            elif hasattr(button, "button"):
                decorate(button.button)
            elif hasattr(button, "style") and hasattr(button, "text"):
                style = copy.copy(button.style) if button.style else types.KeyboardButtonStyle()
                if plain or self.disabled:
                    style.icon = None
                elif not style.icon:
                    matches = [
                        target
                        for match in BUTTON_EMOJI.finditer(button.text)
                        if (
                            target := available.get(emoji_key(match.group()))
                            or fallbacks.get(emoji_key(match.group()))
                        )
                        is not None
                    ]
                    chosen = (
                        matches[0]
                        if matches
                        else next((alt for alt in ("⚙️", "▶️", "📥") if alt in icons), None)
                    )
                    style.icon = icons.get(chosen)
                button.style = style
                if style.icon:
                    label = button_label(button.text)
                    if label == button.text and BUTTON_EMOJI.search(label):
                        style.icon = None
                    button.text = label

        decorate(buttons)
        return buttons

    async def _send(self, sender, text: str, options):
        was_disabled = self.disabled
        options = dict(options)
        original_buttons = options.get("buttons")
        if "buttons" in options:
            options["buttons"] = self.buttons(options["buttons"])
        if "formatting_entities" in options:
            entities = options.pop("formatting_entities")
        else:
            text, entities = self.prepare(text)
        if self.disabled:
            entities = [
                item for item in entities if not isinstance(item, types.MessageEntityCustomEmoji)
            ]
        try:
            return await sender(
                text, **({"formatting_entities": entities} if entities else {}), **options
            )
        except errors.RPCError as error:
            if self.disabled and not was_disabled:
                raise
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
            if "buttons" in options:
                options["buttons"] = self.buttons(original_buttons, plain=True)
            plain_entities = [
                item for item in entities if not isinstance(item, types.MessageEntityCustomEmoji)
            ]
            return await sender(
                text,
                **({"formatting_entities": plain_entities} if plain_entities else {}),
                **options,
            )

    async def respond(self, event, text: str, **options):
        options.setdefault("parse_mode", None)
        options.setdefault("reply_to", getattr(event, "id", None))
        message = await self._send(event.respond, text, options)
        return PresentedMessage(message, self) if hasattr(message, "edit") else message

    async def edit(self, message, text: str, **options):
        options.setdefault("parse_mode", None)
        return await self._send(message.edit, text, options)

    async def send_file(self, client, peer, file, **options):
        caption = options.pop("caption", "")
        options.setdefault("parse_mode", None)

        async def send(text, **prepared):
            return await client.send_file(peer, file, caption=text, **prepared)

        return await self._send(send, caption, options)


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
