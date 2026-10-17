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


async def reset_callback(event, admin, action, value):
    from bot.handlers.admin_render import source_reply
    from bot.keyboards.admin import button

    await admin.authorize(event.sender_id, root=True)
    reset = admin.maintenance.database_reset
    if reset is None:
        raise DownloadError("پاکسازی کامل در این اجرا آماده نیست؛ ربات را به‌روز و دوباره اجرا کن.")
    if action == "resetprepare":
        token, counts = await reset.prepare(event.sender_id)
        await source_reply(
            event,
            "⚠️ پاکسازی کامل دیتابیس\n\n"
            f"تمام داده‌های {len(counts)} جدول برنامه برای همهٔ ربات‌های این دیتابیس حذف می‌شوند. "
            f"تعداد فعلی: {sum(counts.values()):,} رکورد.\n\n"
            "کاربران و ادمین‌های ثبت‌شده، گروه‌ها و اعضا، پیام‌ها و تاریخچه، فایل‌های ذخیره‌شده، "
            "تنظیمات، حساب‌های دانلودر و عملیات در انتظار پاک می‌شوند؛ کش و صف‌های Redis همین داده‌ها هم پاک می‌شوند.\n"
            "این حذف برگشت‌پذیر نیست. اگر لازم داری، پیش از تأیید از PostgreSQL بک‌آپ کامل بگیر.\n\n"
            "ساختار جدول‌ها و سابقهٔ مهاجرت حفظ می‌شوند. فایل .env، نشست محلی و پیام‌های داخل تلگرام حذف نمی‌شوند.\n"
            "پس از پاکسازی، ربات متوقف می‌شود. توکن جدید را در .env بگذار و python main.py را اجرا کن.\n\n"
            "حذف تمام داده‌ها و توقف ربات را تأیید می‌کنی؟",
            buttons=[
                [button(event.sender_id, "❌ انصراف", "resetcancel", token)],
                [
                    button(
                        event.sender_id, "🗑 بله، حذف تمام داده‌ها و توقف ربات", "resetconfirm", token
                    )
                ],
            ],
            edit=getattr(event, "data", None) is not None,
        )
    elif action == "resetcancel":
        await reset.cancel(event.sender_id, value)
        await source_reply(event, "پاکسازی کامل لغو شد.", buttons=None, edit=True)
    elif action == "resetconfirm":
        try:
            counts = await reset.confirm(event.sender_id, value)
            await source_reply(
                event,
                f"✅ پاکسازی کامل انجام شد: {len(counts)} جدول برنامه و کش و صف‌های مربوطه خالی شدند.\n"
                "ربات متوقف می‌شود. توکن را در .env تنظیم کن و python main.py را دوباره اجرا کن.",
                buttons=None,
                edit=True,
            )
        except Exception:
            if reset.stopped:
                await source_reply(
                    event,
                    "⚠️ پاکسازی کامل تأیید نشد و ربات برای جلوگیری از ثبت مجدد داده متوقف می‌شود. "
                    "پس از بررسی لاگ و اتصال دیتابیس و Redis، دوباره اجرا کن؛ پاکسازی ناتمام Redis پیش از شروع ربات بازیابی می‌شود.",
                    buttons=None,
                    edit=True,
                )
            else:
                await source_reply(
                    event,
                    "⚠️ پاکسازی کامل انجام نشد و داده‌ها حذف نشدند. اتصال دیتابیس و Redis و لاگ خطا را بررسی کن و دوباره تأیید بگیر.",
                    buttons=None,
                    edit=True,
                )
            raise
        finally:
            if reset.stopped:
                await reset.shutdown()


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
