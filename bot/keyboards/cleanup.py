"""Native inline cleanup controls mirroring the Couplyo picker/builder/confirm flow."""

from bot.keyboards.admin import button
from bot.texts import cleanup as text
from core.cleanup import ENTITIES, encode


def picker(owner):
    return [
        [button(owner, text.ENTITIES[ent.code][0], "cleanfilter", encode(ent, ent.default))]
        for ent in ENTITIES
    ] + [
        [button(owner, "🗑 پاکسازی کامل دیتابیس", "resetprepare")],
        [button(owner, "✖️ بستن", "cleanclose")],
    ]


def builder(owner, ent, masks, *, deletable):
    rows = []
    for ai, (attr, mask) in enumerate(zip(ent.attributes, masks, strict=True)):
        cells = []
        for ci, label in enumerate(text.ATTRS[attr.code][1]):
            changed = list(masks)
            changed[ai] ^= 1 << ci
            cells.append(
                button(
                    owner,
                    f"{'✅' if mask & (1 << ci) else '⬜️'} {label}",
                    "cleanfilter",
                    encode(ent, changed),
                )
            )
        per_row = 2 if attr.n == 4 else 3
        rows.extend(cells[index : index + per_row] for index in range(0, len(cells), per_row))
    value = encode(ent, masks)
    rows.append(
        [
            button(owner, "♻️ همه‌حالت‌ها", "cleanall", value),
            button(owner, "🔄 پیش‌فرض", "cleandefault", value),
        ]
    )
    if deletable:
        rows.append([button(owner, "🗑 حذفِ موارد بالا", "cleango", value)])
    rows.append(
        [
            button(owner, "↩️ بخش‌های دیگر", "cleanpick"),
            button(owner, "✖️ بستن", "cleanclose"),
        ]
    )
    return rows


def confirm(owner, token, ent, masks):
    return [
        [
            button(owner, "❌ نه، برگرد", "cleanfilter", encode(ent, masks)),
            button(owner, "✅ بله، حذف کن", "cleanup", ent.code + "." + token),
        ]
    ]
