"""Owner-bound cleanup preview, filter and explicit-confirmation adapters."""

from bot.keyboards.admin import button
from schemas.media import DownloadError

DEFAULTS = {
    "users": {"started": "no", "membership": "yes", "kind": "all"},
    "groups": {"admin": "all", "members": "all", "owner": "all"},
}
CHOICES = {
    "started": ("all", "yes", "no"),
    "membership": ("all", "yes", "no"),
    "kind": ("all", "real", "unknown", "bot", "deleted", "invalid"),
    "admin": ("all", "yes", "no"),
    "members": ("all", "yes", "no"),
    "owner": ("all", "yes", "no"),
}
LABELS = {
    "started": "استارت",
    "membership": "سابقه عضویت",
    "kind": "نوع حساب",
    "admin": "ادمینی",
    "members": "عضو ثبت‌شده",
    "owner": "مالک معلوم",
}
VALUES = {
    "all": "همه",
    "yes": "بله",
    "no": "خیر",
    "real": "انسان",
    "unknown": "نامعلوم",
    "bot": "بات",
    "deleted": "دیلت‌اکانت",
    "invalid": "بات یا دیلت‌اکانت",
}


def encode(entity, filters):
    return (
        ("u" if entity == "users" else "g")
        + "_"
        + "".join(str(CHOICES[key].index(value)) for key, value in filters.items())
    )


def decode(value):
    if not isinstance(value, str) or len(value) < 3 or value[1] != "_":
        raise DownloadError("فیلتر معتبر نیست.")
    entity = "users" if value[0] == "u" else "groups" if value[0] == "g" else None
    if entity is None:
        raise DownloadError("فیلتر معتبر نیست.")
    raw = value.split("_")[1]
    if len(raw) != len(DEFAULTS[entity]):
        raise DownloadError("فیلتر معتبر نیست.")
    try:
        return entity, {
            key: CHOICES[key][int(index)] for key, index in zip(DEFAULTS[entity], raw, strict=True)
        }
    except (ValueError, IndexError) as error:
        raise DownloadError("فیلتر معتبر نیست.") from error


async def open_cleanup(event, admin, deleted_only=False, value=None):
    await admin.authorize(event.sender_id, root=True)
    entity, filters = (
        decode(value)
        if value
        else (
            "users",
            {
                **DEFAULTS["users"],
                **(
                    {"kind": "invalid", "started": "all", "membership": "all"}
                    if deleted_only
                    else {}
                ),
            },
        )
    )
    preview = await admin.maintenance.cleanup_preview(event.sender_id, entity, filters)
    text = f"🧹 پاکسازی {entity}\nتعداد منطبق: {preview['matched']} · سقف هر اجرا: ۱۰۰۰\nفقط کاربران خارج از گروه فعال یا گروه‌های قطعاً خارج‌شده حذف می‌شوند؛ مدیران محافظت می‌شوند. عضویت و شواهد وابسته نیز حذف می‌شوند. تاریخچه دریافت فایل حفظ می‌شود."
    buttons = [
        [
            button(event.sender_id, "کاربران", "cleanview", encode("users", DEFAULTS["users"])),
            button(event.sender_id, "گروه‌ها", "cleanview", encode("groups", DEFAULTS["groups"])),
        ]
    ]
    for key, current in filters.items():
        values = CHOICES[key]
        changed = {**filters, key: values[(values.index(current) + 1) % len(values)]}
        buttons.append(
            [
                button(
                    event.sender_id,
                    f"{LABELS[key]}: {VALUES[current]}",
                    "cleanview",
                    encode(entity, changed),
                )
            ]
        )
    buttons.append(
        [button(event.sender_id, "⚠️ آماده‌سازی حذف", "cleanprepare", encode(entity, filters))]
    )
    from bot.handlers.admin import show

    await show(event, text, buttons)
