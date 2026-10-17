"""Couplyo group-event copy; matching-specific promises are excluded."""

import html
from datetime import datetime

from core.jalali import to_persian_digits as pd
from core.jalali import to_shamsi

LAZY = "🐢 <i>در بررسی وضعیت فعلی شناسایی شد؛ رویداد مستقیم این تغییر ثبت نشده بود.</i>"


def record(group):
    rows = []
    for key, label in (
        ("title", "نام"),
        ("member_count", "اعضا (تلگرام)"),
        ("members_in_db", "عضویتِ ثبت‌شده پیشِ ما"),
        ("link", "لینک"),
        ("owner_id", "مالک"),
    ):
        value = group.get(key)
        if value is not None:
            shown = (
                pd(value)
                if key in {"member_count", "members_in_db"}
                else html.escape(str(value)[:200])
            )
            if key == "owner_id":
                shown = f"<code>{value}</code>"
            rows.append(f"• {label}: {shown}")
    if group.get("admin_ids"):
        rows.append(f"• ادمین‌های دیگر: {pd(len(group['admin_ids']))}")
    rows.append("• ادمین بودم: " + ("بله" if group.get("is_admin") else "نه"))
    if group.get("can_send") is not None:
        rows.append("• اجازه‌ی ارسال: " + ("داشتم" if group["can_send"] else "نداشتم"))
    if group.get("added_at"):
        rows.append("• از: ⁦" + to_shamsi(datetime.fromisoformat(group["added_at"])) + "⁩")
    return "\n".join(rows)


def admin_event(data):
    kind, group = data["kind"], data["group"]
    chat_id = group["chat_id"]
    who = (
        f"«{html.escape(group['title'][:200])}»"
        if group.get("title")
        else f"<code>{chat_id}</code>"
    )
    body = {
        "added": f"➕ به گروهِ {who} <b>اضافه</b> شدم (هنوز ادمین نیستم).",
        "admin": f"🛡 تو گروهِ {who} <b>ادمین</b> شدم.",
        "removed": f"➖ از گروهِ {who} <b>حذف/اخراج</b> شدم و غیرفعالش کردم.",
        "demoted": f"⚠️ تو گروهِ {who} از <b>ادمینی برداشته</b> شدم.",
        "migrated": f"🔁 گروهِ {who} به <b>سوپرگروه</b> تبدیل شد؛ تلگرام شناسه‌ش رو عوض کرد و تنظیماتش رو به شناسه‌ی جدید منتقل کردم.",
        "discovered": f"🔎 گروهِ {who} رو <b>شناسایی</b> کردم و به دیتابیس اضافه شد، از قبل عضوش بودم و از روی پیام‌ها فهمیدم.\n{LAZY}",
        "reactivated": f"🔄 گروهِ {who} که قبلاً ازش حذف شده بودم <b>دوباره فعال</b> شد؛ از روی پیام‌ها فهمیدم برگشتم توش.\n{LAZY}",
        "admin_lazy": f"🛡 تو گروهِ {who} <b>ادمین</b> شدم.\n{LAZY}",
        "demoted_lazy": f"⚠️ تو گروهِ {who} از <b>ادمینی برداشته</b> شدم.\n{LAZY}",
        "removed_lazy": f"➖ دیدم که دیگه <b>عضوِ</b> گروهِ {who} نیستم، پس غیرفعالش کردم.\n{LAZY}",
    }.get(kind, f"وضعیتم تو گروهِ {who} عوض شد.")
    text = f"🔔 <b>اعلانِ گروه</b>\n{body}\n🆔 <code>{chat_id}</code>"
    if kind in {"removed", "removed_lazy"}:
        text += "\n\n<b>آخرین چیزی که ازش داشتیم:</b>\n" + record(group)
    return text


def announcement(kind, group, *, seed=0):
    if kind == "admin":
        variants = [
            "ادمین شدم، ممنون ✅\n"
            "از این به بعد فهرست اعضا رو هم می‌تونم ببینم. "
            "برای دانلود، لینک پست رو بفرستید و کیفیت رو انتخاب کنید.\n\n"
            "راهنمای اعضا و ادمین‌ها: /help",
            "دسترسی رسید، مرسی 🙌\n"
            "فهرست اعضا هم در دسترسم قرار گرفت. "
            "هر کی فایل می‌خواد، لینکش رو همین‌جا بفرسته.\n\n"
            "راهنمای اعضا و ادمین‌ها: /help",
        ]
        return variants[seed % len(variants)]
    if kind == "demoted":
        owner = group.get("owner_id")
        mention = f'<a href="tg://user?id={owner}">مالک گروه</a> ' if owner else ""
        return "⚠️ دیگه ادمین نیستم.\n" + mention + "برای دیدنِ فهرست اعضا، دوباره ادمینم کن 🙏"
    return (
        "سلام به همه 👋 من ربات دانلودم.\n"
        "لینک پست رو همین‌جا بفرستید؛ کیفیت‌های قابل دریافت رو نشون می‌دم "
        "و هر کدوم رو انتخاب کنید، فایلش رو می‌فرستم.\n\n"
        "برای دیدنِ فهرست اعضا هم ادمینم کنید.\n\nراهنما: /help"
    )
