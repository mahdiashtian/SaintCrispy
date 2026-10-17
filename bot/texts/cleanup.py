"""Couplyo cleanup wording with downloader-specific deletion and retention scope."""

from bot.texts.couplyo_admin import pd

PICK = (
    "<b>🧹 پاکسازی دیتابیس</b> ⚠️\n"
    "می‌خوای کدوم بخش رو پاکسازی کنی؟ بعدش با دکمه‌های شیشه‌ای فیلترش رو "
    "دقیق می‌کنی و <b>قبل از حذف</b> هم توضیح و هم تعدادِ مواردِ شامل رو می‌بینی 👇"
)
CLOSED = "بسته شد ✅ هیچی حذف نشد."
NOTHING = "با این فیلتر هیچ موردی پیدا نشد، چیزی برای حذف نیست ✅"
HINT = "<i>با دکمه‌ها فیلتر رو عوض کن، همین متن و تعداد هم آپدیت می‌شه.</i>"
ENTITIES = {"u": ("👥 پاکسازی کاربران", "کاربر"), "g": ("🏠 پاکسازی گروه‌ها", "گروه")}
ATTRS = {
    "st": ("وضعیت استارت", ("استارت‌زده", "استارت‌نزده")),
    "mb": ("سابقه‌ی گروه", ("از گروه اومده", "بی‌سابقه‌ی گروه")),
    "ak": ("نوع حساب", ("انسان", "نامعلوم", "بات", "دیلت‌اکانت")),
    "gad": ("ادمینی", ("ادمین هستیم", "ادمین نیستیم")),
    "gmb": ("اعضا", ("عضو داره", "عضو نداره")),
    "gow": ("مالک", ("مالک شناخته‌شده", "مالک نامشخص")),
}


def briefing(ent, masks, preview, *, confirm=False):
    title, noun = ENTITIES[ent.code]
    lines = [f"<b>{title}</b>\n\n<b>🎯 فیلترِ فعلی</b>"]
    for attr, mask in zip(ent.attributes, masks, strict=True):
        label, choices = ATTRS[attr.code]
        selected = [label for index, label in enumerate(choices) if mask & (1 << index)]
        value = (
            "<i>هر حالتی، بی‌تأثیر</i>"
            if mask == attr.full
            else "، ".join(selected)
            if selected
            else "<b>هیچ‌کدام ❗️</b>"
        )
        lines.append(f"• {label}: {value}")
    if 0 in masks:
        empty = next(
            ATTRS[attr.code][0]
            for attr, mask in zip(ent.attributes, masks, strict=True)
            if not mask
        )
        lines.append(
            f"\n🚫 <b>برای «{empty}» هیچ گزینه‌ای روشن نیست</b>\n\n"
            "با این فیلتر هیچ موردی پیدا نمی‌شه، پس چیزی هم حذف نمی‌شه. حداقل یکی از "
            f"گزینه‌های «{empty}» رو روشن کن."
        )
    else:
        lines += [
            "\n<b>❌ اگه تأیید کنی، این‌ها حذف می‌شن:</b>",
            f"• {noun}های منطبق با فیلتر و عضویت‌ها و شواهد وابسته‌شون",
            "\n<b>✅ این‌ها دست‌نخورده می‌مونن:</b>",
            "• هر موردی که با فیلتر هم‌خوان نباشه",
            "• ادمین‌ها، ادمین اصلی و کاربرانِ عضو گروه فعال"
            if ent.code == "u"
            else "• خودِ کاربران، گروه‌های فعال و گروه‌های با وضعیت نامعلوم یا مهاجرت‌کرده",
            "• تاریخچهٔ دریافت فایل، تنظیمات و حساب‌های دانلودر",
            f"\n<b>📊 الان شاملِ:</b> <b>{pd(preview['matched'])}</b> {noun} از مجموعِ {pd(preview['total'])}",
            f"• سقف این اجرا: {pd(preview['batch'])} مورد",
            "\n⚠️ حذف <b>هارد دیلت</b> و <b>برگشت‌ناپذیره</b>: با بازیابیِ بک‌آپ فقط برمی‌گرده.",
        ]
        if confirm:
            lines = ["⚠️ <b>تأییدِ نهایی</b>\n", *lines, "\n<b>تأیید می‌کنی؟</b>"]
    if not confirm:
        lines.append("\n" + HINT)
    return "\n".join(lines)
