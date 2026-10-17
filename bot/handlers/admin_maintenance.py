"""Owner-bound cleanup preview, filter and explicit-confirmation adapters."""

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
    from bot.handlers.admin_render import source_reply
    from bot.keyboards.cleanup import picker
    from bot.texts.cleanup import PICK

    await source_reply(
        event, PICK, buttons=picker(event.sender_id), edit=getattr(event, "data", None) is not None
    )


async def cleanup_callback(event, admin, action, value):
    from bot.handlers.admin_render import source_reply
    from bot.keyboards import cleanup as keyboard
    from bot.texts import cleanup as text
    from core.cleanup import decode as decode_masks
    from core.cleanup import filters

    await admin.authorize(event.sender_id, root=True)
    if action == "cleanclose":
        await source_reply(event, text.CLOSED, buttons=None, edit=True)
        return
    if action == "cleanpick":
        await open_cleanup(event, admin)
        return
    ent, masks = decode_masks(value)
    if action == "cleandefault":
        masks = ent.default
    elif action == "cleanall":
        masks = tuple(attr.full for attr in ent.attributes)
    selected = filters(ent, masks)
    if action == "cleango":
        token, preview = await admin.maintenance.prepare_cleanup(
            event.sender_id, ent.name, selected
        )
        buttons = keyboard.confirm(event.sender_id, token, ent, masks)
    else:
        preview = await admin.maintenance.cleanup_preview(event.sender_id, ent.name, selected)
        buttons = keyboard.builder(
            event.sender_id, ent, masks, deletable=bool(preview["matched"]) and 0 not in masks
        )
    await source_reply(
        event,
        text.briefing(ent, masks, preview, confirm=action == "cleango"),
        buttons=buttons,
        edit=True,
    )
