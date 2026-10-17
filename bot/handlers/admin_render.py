"""Render canonical HTML with compatible premium-emoji entities."""

import copy

from telethon import errors, types
from telethon.extensions import html
from telethon.helpers import add_surrogate, del_surrogate


async def source_reply(event, markup, *, buttons=None, presentation=None, edit=False, **options):
    if presentation is not None:
        markup, _ = presentation.prepare(markup)
    text, entities = html.parse(markup)
    custom = presentation.entities(text) if presentation is not None else []
    chunks = split_entities(text, [*entities, *custom])
    result = None
    for index, (chunk, formatting) in enumerate(chunks):
        sender = event.edit if edit and index == 0 else event.respond
        prepared = {
            "parse_mode": None,
            "link_preview": False,
            "buttons": buttons if index == len(chunks) - 1 else None,
            **options,
        }
        if presentation is not None and presentation.disabled:
            formatting = [
                item for item in formatting if not isinstance(item, types.MessageEntityCustomEmoji)
            ]
        try:
            if (
                callable(getattr(presentation, "_send", None))
                and getattr(event, "presentation", None) is not presentation
            ):
                result = await presentation._send(
                    sender, chunk, {"formatting_entities": formatting, **prepared}
                )
            else:
                result = await sender(chunk, formatting_entities=formatting, **prepared)
        except errors.RPCError as error:
            if (
                (presentation is not None and presentation.disabled)
                or not custom
                or type(error).__name__
                not in {
                    "CustomEmojiInvalidError",
                    "EntityBoundsInvalidError",
                    "EntityTypeInvalidError",
                    "PremiumAccountRequiredError",
                    "EntitiesTooLongError",
                    "DocumentInvalidError",
                }
            ):
                raise
            presentation.disabled = True
            result = await sender(
                chunk,
                formatting_entities=[
                    item
                    for item in formatting
                    if not isinstance(item, types.MessageEntityCustomEmoji)
                ],
                **prepared,
            )
    return result


def split_entities(text, entities, limit=4000):
    """Split parsed text in UTF-16 units while preserving entity spans."""
    encoded, start, result = add_surrogate(text), 0, []
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        if end < len(encoded):
            newline = encoded.rfind("\n", start + limit // 2, end)
            if newline >= 0:
                end = newline + 1
            if 0xD800 <= ord(encoded[end - 1]) <= 0xDBFF:
                end -= 1
            for item in entities:
                if (
                    isinstance(item, types.MessageEntityCustomEmoji)
                    and item.offset < end < item.offset + item.length
                ):
                    end = item.offset
        formatting = []
        for item in entities:
            left, right = max(start, item.offset), min(end, item.offset + item.length)
            if left < right:
                clone = copy.copy(item)
                clone.offset, clone.length = left - start, right - left
                formatting.append(clone)
        result.append((del_surrogate(encoded[start:end]), formatting))
        start = end
    return result
