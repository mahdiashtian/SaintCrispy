"""Canonical non-matching Couplyo administrator copy, independently packaged.

User-facing wording is retained from the supplied reference. Downloader-only
extensions and safer additive backups have explicitly separate copy.
"""

import html
from types import SimpleNamespace
from typing import Final

from bot.texts import admin_buttons as labels
from core.jalali import to_shamsi

buttons = SimpleNamespace(STORAGE_KIND_LABELS=labels.KINDS, JOIN_POINT_LABELS=labels.POINTS)


def pd(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def pdn(value):
    return pd(f"{value:,}").replace(",", "٬")


def downloader_user_trace(data, *, with_history=True):
    from core.jalali import to_shamsi

    user = data["profile"]
    if not user:
        return ADMIN_USER_LOOKUP_INVALID

    def yn(value):
        return "بله ✅" if value else "خیر ❌"

    def stamp(value):
        return to_shamsi(value) if value else "نامشخص"

    identity = user["user_id"]
    name = html.escape(user["first_name"] or "کاربر")
    lines = [
        "<b>پروفایلِ کاربر</b> 🔎",
        f'<a href="tg://user?id={identity}">👤 {name}</a>',
        f"🆔 <code>{identity}</code>",
        f"🔗 یوزرنیم: {(html.escape('@' + user['username']) if user['username'] else 'نامشخص')}",
        "➖➖➖➖➖➖➖➖➖➖",
        f"▶️ استارت زده: {yn(user['is_started'])}",
        f"🚫 ربات رو بلاک کرده: {yn(user['is_blocked'])}",
        f"🛡 ادمینِ ربات: {yn(user['is_admin'])}",
        f"⛔️ مسدود شده: {yn(user['is_banned'])}",
        f"🕐 اولین مشاهده: {stamp(user['first_seen_at'])}",
        f"▶️ اولین استارت: {stamp(user['started_at'])}",
        f"🕐 آخرین مشاهده: {stamp(user['last_seen_at'])}",
        f"💬 آخرین فعالیت: {stamp(user['last_active_at'])}",
    ]
    if not with_history:
        return "\n".join(lines)
    groups = data["groups"]
    total = groups[0]["total_groups"] if groups else 0
    active = groups[0]["active_groups"] if groups else 0
    lines += [
        "➖➖➖➖➖➖➖➖➖➖",
        f"<b>گروه\u200cها</b> 👥 · عضوِ فعال: {pd(active)} از {pd(total)}",
    ]
    if not groups:
        lines.append("توی هیچ گروهی دیده نشده 📭")
    for group in groups:
        lines.append(
            f"{('🟢' if group['is_active'] else '⚪️')} <b>{html.escape(group['title'] or 'بدون\u200cنام')}</b>\n   <code>{group['chat_id']}</code> • آخرین فعالیت: {stamp(group['last_seen_at'])}"
        )
    if total > len(groups):
        lines.append(f"… و {pd(total - len(groups))} گروهِ دیگر")
    lines += ["➖➖➖➖➖➖➖➖➖➖", "<b>ده دریافت اخیر</b> 📥"]
    for row in data["downloads"]:
        lines.append(
            f"• {html.escape(row['site'])} · <code>{html.escape(row['content_id'])}</code> · {html.escape(row['quality'])} · {pd(row['delivery_count'])} بار"
        )
    return "\n".join(lines)


def downloader_daily_stats(data):
    lines = [
        "<b>آمارِ زندهٔ امروز</b> 📊",
        f"📅 {pd(data['stat_date'].isoformat())}",
        "",
        "<b>👥 کاربران</b>",
    ]
    for key, label in (
        ("new_users", "کاربرانِ تازه شناسایی\u200cشده"),
        ("first_starts", "اولین استارت"),
        ("bot_active_users", "کاربرانِ فعالِ خود ربات"),
        ("active_senders", "کاربرانِ یکتا که تو گروه\u200cهای ادمین پیام دادن"),
    ):
        lines.append(f"• {label}: {pdn(data.get(key, 0))}")
    lines += ["", "<b>🏠 گروه\u200cها</b>"]
    for key, label in (
        ("groups_added", "اضافه\u200cشده"),
        ("groups_removed", "حذف\u200cشده"),
        ("groups_admin_gained", "ادمین شدم"),
        ("groups_admin_lost", "از ادمینی دراومدم"),
    ):
        lines.append(f"• {label}: {pdn(data.get(key, 0))}")
    lines += ["", "<b>🚀 منبع اولین استارت</b>"]
    for key, label in (
        ("referral_starts", "دعوت"),
        ("plain_starts", "استارت معمولی"),
        ("unknown_starts", "منبع نامشخص"),
    ):
        lines.append(f"• {label}: {pdn(data.get(key, 0))}")
    lines += [
        "",
        "<b>📦 وضعیتِ کلی (تا این لحظه)</b>",
        f"• کلِ کاربران: {pdn(data.get('total_users', 0))}",
        f"• کلِ گروه\u200cها: {pdn(data.get('total_groups', 0))} (فعال: {pdn(data.get('active_groups', 0))} • ادمین: {pdn(data.get('admin_groups', 0))})",
    ]
    return "\n".join(lines)


def downloader_daily_history(rows, *, page=0, pages=None):
    if not rows:
        return "<b>تاریخچه\u200cی آمار</b> 📈\n\nهنوز هیچ روزی ثبت نشده."
    lines = [
        "<b>تاریخچه\u200cی آمار</b> 📈",
        "<i>روز: شناسایی\u200cشده • پیام\u200cدهندهٔ گروه</i>",
        "<i>جذب و فعالیت: 🚀 اولین استارت • 🤖 فعال ربات (— یعنی ثبت نشده)</i>",
        "<i>ادمینی: ⬆️ ادمین شدم • ⬇️ از ادمینی دراومدم</i>",
        "➖➖➖➖➖➖➖➖➖➖",
    ]
    if pages is not None:
        lines.insert(1, f"صفحه {pdn(page + 1)} از {pdn(pages)}")
    for row in rows:
        lines.append(
            f"📅 {pd(row['stat_date'].isoformat())}\n   👥 {pdn(row['new_users'])} • 💬 {pdn(row['active_senders'])}\n   🚀 {pdn(row['first_starts'])} • 🤖 {pdn(row['bot_active_users'])}\n   ⬆️ {pdn(row['groups_admin_gained'])} • ⬇️ {pdn(row['groups_admin_lost'])}"
        )
    return "\n".join(lines)


CHOOSE_FROM_LIST = "یکی از دکمه\u200cهای پایین رو بزن 👇"
ADMIN_DEV_PRIMARY_ONLY = "فقط ادمینِ اصلی می\u200cتونه آیدیِ توسعه\u200cدهنده رو تنظیم کنه ⛔️"
ADMIN_DEV_INVALID = "چیزی دریافت نشد 🤔 یه متن بفرست تا ذخیره\u200cش کنم."
ADMIN_DEV_CLEARED = "آیدیِ توسعه\u200cدهنده پاک شد ✅"


def admin_developer_prompt(current: str | None) -> str:
    now = (
        f"مقدارِ فعلی: {html.escape(current)}"
        if current
        else "الان چیزی تنظیم نشده (از مقدارِ پیش\u200cفرض استفاده می\u200cشه)."
    )
    return f"<b>تنظیمِ آیدیِ توسعه\u200cدهنده</b> 👨\u200d💻\n\n{now}\n\nهرچی بفرستی (آیدی، یوزرنیم، اسم، هر متنی) همون عیناً ذخیره می\u200cشه و در بخشِ «سازنده» نمایش داده می\u200cشه."


def admin_developer_set(value: str) -> str:
    return f"آیدیِ توسعه\u200cدهنده ذخیره شد ✅\n{html.escape(value)}"


def admin_panel(name: str | None) -> str:
    who = html.escape(name or "ادمین")
    return f"<b>پنل مدیریت</b> 🛠\nخوش اومدی {who}. یکی از دسته\u200cها رو انتخاب کن 👇"


ADMIN_CAT_ADMINS_PANEL = (
    "<b>👤 مدیریت ادمین\u200cها</b>\nافزودن، حذف و دیدنِ لیستِ ادمین\u200cها. یکی رو انتخاب کن 👇"
)
ADMIN_CAT_MSG_PANEL = "<b>📢 پیام\u200cرسانی</b>\nپیام/فورواردِ همگانی به کاربرای استارت\u200cزده یا گروه\u200cهای فعال، به\u200cعلاوه\u200cی ارسال تکی. یکی رو انتخاب کن 👇"
ADMIN_CAT_CHANNELS_PANEL = "<b>🔗 کانال\u200cها و تبلیغات</b>\nعضویتِ اجباری، کانالِ آرشیو و پیامِ تبلیغیِ گروه\u200cها. یکی رو انتخاب کن 👇"
ADMIN_CAT_DATA_PANEL = "<b>📊 آمار و دیتابیس</b>\nآمارِ کلی، جستجوی کاربر، بک\u200cآپ و بازیابیِ دیتابیس. یکی رو انتخاب کن 👇"
ADMIN_ADD_PROMPT = "آیدی عددیِ کسی که می\u200cخوای ادمین بشه رو بفرست 👇"
ADMIN_REMOVE_PROMPT = "آیدی عددیِ ادمینی که می\u200cخوای حذف بشه رو بفرست 👇"
ADMIN_INVALID_ID = "آیدی نامعتبره؛ فقط عدد بفرست 🔢"
ADMIN_USER_NOT_FOUND = "همچین کاربری پیدا نشد 🤔"
ADMIN_ADDED = "✅ ادمین اضافه شد."
ADMIN_REMOVED = "✅ ادمین حذف شد."
ADMIN_LIST_HEADER = "<b>ادمین\u200cها:</b>\n\n"
ADMIN_LIST_EMPTY = "هنوز ادمینی ثبت نشده."
ADMIN_BROADCAST_PROMPT = "پیامی که می\u200cخوای برای کاربرای استارت\u200cزده کپی بشه رو بفرست 👇"
ADMIN_FORWARD_PROMPT = "پیامی که می\u200cخوای برای کاربرای استارت\u200cزده فوروارد بشه رو بفرست 👇"
_GROUP_AUDIENCE_LABEL: dict[str, str] = {
    "both": "همه\u200cی گروه\u200cهای فعال",
    "admin": "فقط گروه\u200cهایی که توشون ادمینیم",
    "non_admin": "فقط گروه\u200cهایی که توشون ادمین نیستیم",
}


def admin_group_audience_ask(mode: str) -> str:
    """Step 1: which groups should this copy/forward reach?"""
    verb = "کپی" if mode == "copy" else "فوروارد"
    return f"این پیام رو کجا {verb} کنم؟ 👇"


ADMIN_GROUP_EXCLUDE_CHATS_PROMPT = "آیدی گروه\u200cهایی که <b>نباید این پیام رو بگیرن</b> رو بفرست؛ هر آیدی توی یک خط 👇\n\n<pre>-1003954720416\n-1003954720417</pre>\nبعدش می\u200cتونی آیدی کاربران رو هم برای حذف گروه\u200cهاشون وارد کنی.\nاگر گروهی رو نمی\u200cخوای حذف کنی، «بدون استثنا در این مرحله» رو بزن."
ADMIN_GROUP_EXCLUDE_USERS_PROMPT = "آیدی عددی کاربران رو بفرست؛ هر آیدی توی یک خط 👇\n\n<pre>123456789\n987654321</pre>\nهر گروهی که <b>حداقل یکی</b> از این افراد عضو فعالش باشه، پیام نمی\u200cگیره.\nاین بررسی براساس عضویت ثبت\u200cشده در دیتابیس رباته؛ عضویت\u200cهای ناشناخته قابل تشخیص نیستن.\nاستثناهای گروهی مرحله قبل هم اعمال می\u200cشن.\nاگر این فیلتر رو نمی\u200cخوای، «بدون استثنا در این مرحله» رو بزن."
ADMIN_GROUP_EXCLUDE_CHATS_INVALID = "لیست معتبر نیست. هر خط فقط یک آیدی منفی گروه مثل -1003954720416 باشه؛ بدون نام، لینک یا جداکننده. لیست رو کامل دوباره بفرست."
ADMIN_GROUP_EXCLUDE_USERS_INVALID = "لیست معتبر نیست. هر خط فقط یک آیدی عددی مثبت کاربر مثل 123456789 باشه؛ بدون نام، لینک یا جداکننده. لیست رو کامل دوباره بفرست."


def admin_group_exclusions_summary(chat_count: int, user_count: int) -> str:
    return f"استثناها: {pd(chat_count)} آیدی گروه و گروه\u200cهای {pd(user_count)} آیدی کاربر.\nاین تنظیمات فقط برای همین ارسال اعمال می\u200cشن."


def admin_group_broadcast_prompt(
    mode: str, audience: str | None, *, chat_count: int = 0, user_count: int = 0
) -> str:
    """Ask for content only after both exclusion lists have been chosen."""
    verb = "کپی" if mode == "copy" else "فوروارد"
    scope = _GROUP_AUDIENCE_LABEL.get(audience or "both", _GROUP_AUDIENCE_LABEL["both"])
    return f"پیامی که می\u200cخوای توی «{scope}» {verb} بشه رو بفرست 👇\n\n{admin_group_exclusions_summary(chat_count, user_count)}"


def admin_group_audience_label(audience: str | None) -> str:
    return _GROUP_AUDIENCE_LABEL.get(audience or "both", _GROUP_AUDIENCE_LABEL["both"])


def admin_broadcast_sending(n: int) -> str:
    return f"در حال ارسال به {pd(n)} کاربرِ استارت\u200cزده... ⏳"


def admin_group_broadcast_sending(n: int) -> str:
    return f"در حال ارسال به {pd(n)} گروه... ⏳"


def admin_broadcast_done(ok: int, failed: int) -> str:
    return f"تموم شد ✅\nموفق: {pd(ok)} | ناموفق: {pd(failed)}"


ADMIN_TARGET_USER_ID_PROMPT = "آیدیِ عددیِ <b>شخص</b> رو بفرست 🔢"
ADMIN_TARGET_GROUP_ID_PROMPT = "آیدیِ عددیِ <b>گروه</b> رو بفرست 🔢\n(آیدیِ گروه\u200cها معمولاً با <code>-100</code> شروع می\u200cشه)"
ADMIN_TARGET_SEND_PROMPT = "حالا پیامی که می\u200cخوای ارسال بشه رو بفرست 👇 (متن یا عکس/فایل)"
ADMIN_TARGET_FWD_PROMPT = "حالا پیامی که می\u200cخوای فوروارد بشه رو بفرست (یا فوروارد کن) 👇"


def admin_target_sent(ok: bool, *, reason: str | None = None) -> str:
    if ok:
        return "✅ پیام ارسال شد."
    return f"❌ ارسال نشد.\nعلت: {reason or 'نامشخص'}"


ADMIN_BACKUP_CREATING = "در حال تهیه\u200cی بک\u200cآپ... ⏳"
ADMIN_BACKUP_FAILED = "تهیه\u200cی بک\u200cآپ ناموفق بود 😕"
ADMIN_USER_LOOKUP_PROMPT = (
    "آیدیِ عددی یا یوزرنیمِ کاربر رو بفرست (یا روی پیامش ریپلای کن) تا پروفایلش رو نشونت بدم 🔎"
)
ADMIN_USER_LOOKUP_INVALID = "همچین کاربری پیدا نکردم 🤔 آیدی/یوزرنیم رو چک کن."
ADMIN_LOOKUP_AGAIN_HINT = "\n\n🔁 یکی دیگه رو بفرست، یا «بازگشت» رو بزن."
ADMIN_IMPORT_PROMPT = "فایلِ بک\u200cآپ رو بفرست تا بازیابی کنم 📥"
ADMIN_IMPORT_NO_FILE = "فایلی نفرستادی 🤔 یه فایلِ بک\u200cآپ بفرست."
ADMIN_IMPORT_RECEIVING = (
    "دارم فایل رو کامل می\u200cگیرم و بررسی می\u200cکنم... هنوز دیتابیس دست نخورده 📥"
)
ADMIN_IMPORT_INVALID = "این فایل بک\u200cآپ کامل یا سازگار نیست؛ دیتابیس هیچ تغییری نکرد 😕"
ADMIN_IMPORT_FAILED = "بازیابی ناموفق بود و همه\u200cی تغییرات rollback شد؛ دیتابیس قبلی سر جاشه 😕"
ADMIN_IMPORT_DONE = "بازیابی انجام شد و کش هم\u200cگام شد ✅"
ADMIN_PURGE_PRIMARY_ONLY = "این گزینه فقط برای ادمینِ اصلیه 🔒"
ADMIN_DANGER_CANCELLED = "لغو شد ✅ هیچی حذف نشد."
ADMIN_DANGER_RUNNING = "در حال حذف... ⏳"
ADMIN_JOIN_PANEL = "<b>مدیریت عضویت اجباری</b> 📢\nکانال\u200cهای اجباری رو اینجا تنظیم کن."
CHANNEL_ADD_PROMPT = (
    "کانال رو بفرست:\n• یا به شکلِ <code>@username</code>\n• یا دو خط: خط اول لینک، خط دوم آیدیِ عددی"
)
CHANNEL_REMOVE_PROMPT = "لینک یا آیدیِ عددیِ کانالی که می\u200cخوای حذف بشه رو بفرست 👇"
CHANNEL_ADDED = "✅ کانال به لیست اجباری اضافه شد."
CHANNEL_REMOVED = "✅ کانال حذف شد."
CHANNEL_NOT_FOUND = "همچین کانالی توی لیست نبود 🤔"
CHANNEL_LIST_EMPTY = "هیچ کانالِ اجباری\u200cای ثبت نشده."
CHANNEL_LIST_HEADER = "<b>کانال\u200cهای اجباری:</b>\n\n"


def channel_list_item(link: str, id: int) -> str:
    return f"• {html.escape(link)} · <code>{id}</code>\n"


ADMIN_ARCHIVE_PANEL = (
    "<b>کانال آرشیو</b> 🗄\nفعالیت\u200cها به\u200cصورت بی\u200cصدا اونجا لاگ می\u200cشن."
)
ARCHIVE_SET_PROMPT = CHANNEL_ADD_PROMPT
ARCHIVE_SET_DONE = "✅ کانال آرشیو تنظیم شد."
ARCHIVE_REMOVED = "✅ کانال آرشیو حذف شد."
ARCHIVE_NONE = "کانال آرشیوی تنظیم نشده."
ARCHIVE_HEADER = "<b>کانال آرشیو:</b>\n\n"
ADMIN_STORAGE_PANEL = "<b>ذخیره\u200cسازی پیام\u200cها</b> 🗂\nیه کانال ثبت کن و بگو کدوم نوع پیام\u200cها ذخیره بشن؛ بعدش هر پیامِ از اون نوع که توی گروه\u200cها فرستاده بشه، همون\u200cجوری (از طرف خودِ فرستنده) توی اون کانال فوروارد می\u200cشه و یه ریپلای با مشخصاتِ کاملِ فرستنده و گپ می\u200cخوره.\nربات باید توی اون کانال ادمین باشه."
STORAGE_SET_PROMPT = CHANNEL_ADD_PROMPT
STORAGE_SET_DONE = "✅ کانال ذخیره\u200cسازی تنظیم شد."
STORAGE_REMOVED = "✅ کانال ذخیره\u200cسازی حذف شد؛ دیگه هیچ پیامی ذخیره نمی\u200cشه."
STORAGE_NONE = "کانال ذخیره\u200cسازی\u200cای تنظیم نشده."
STORAGE_HEADER = "<b>کانال ذخیره\u200cسازی:</b>\n\n"


def storage_kinds_panel(enabled: frozenset[str] | set[str], *, has_channel: bool) -> str:
    """The type-toggle panel: every kind with its state, and — when no channel is
    registered yet — a warning that the switches do nothing until one is."""
    lines = [
        f"• {label}: " + ("روشن 🟢" if kind in enabled else "خاموش 🔴")
        for kind, label in buttons.STORAGE_KIND_LABELS.items()
    ]
    warning = (
        "" if has_channel else "\n\n⚠️ هنوز کانالی ثبت نشده، برای همین فعلاً هیچی ذخیره نمی\u200cشه."
    )
    return (
        "<b>نوعِ پیام\u200cهایی که ذخیره می\u200cشن</b> 🗂\n\n"
        + "\n".join(lines)
        + warning
        + "\n\nبرای روشن/خاموش\u200cکردن، روی دکمه\u200cی هر کدوم بزن 👇"
    )


def join_points_panel(enabled: frozenset[str] | set[str], *, has_channels: bool) -> str:
    """Render the reference administrator view."""
    lines = [
        f"• {label}: " + ("روشن 🟢" if point in enabled else "خاموش 🔴")
        for point, label in buttons.JOIN_POINT_LABELS.items()
    ]
    warnings = []
    if not has_channels:
        warnings.append(
            "⚠️ هنوز کانالِ اجباری ثبت نشده، پس فعلاً هیچ\u200cکدوم از این\u200cها کاری نمی\u200cکنن."
        )
    others = set(enabled) - {"recheck"}
    if "recheck" not in enabled and others:
        warnings.append(
            "⚠️ «بررسی مجدد» خاموشه ولی جاهای دیگه روشنن، یعنی کاربری که پشتِ دیوار گیر کرده، با زدنِ همون دکمه رد می\u200cشه. عملاً بقیه\u200cی تیک\u200cها دور زده می\u200cشن."
        )
    tail = "\n\n" + "\n\n".join(warnings) if warnings else ""
    return (
        "<b>کجاها عضویتِ اجباری چک بشه</b> 🔗\n\n"
        + "\n".join(lines)
        + tail
        + "\n\nبرای روشن/خاموش\u200cکردن، روی دکمه\u200cی هر کدوم بزن 👇"
    )


def join_point_toggled(point: str, enabled: bool) -> str:
    label = buttons.JOIN_POINT_LABELS.get(point, point)
    state = "روشن شد 🟢" if enabled else "خاموش شد 🔴"
    return f"«{label}» {state}"


def join_all_points_toggled(enabled: bool) -> str:
    return (
        "همه\u200cی نقاط بررسی روشن شدن 🟢"
        if enabled
        else "همه\u200cی نقاط بررسی خاموش شدن 🔴 یعنی عضویتِ اجباری عملاً غیرفعاله."
    )


def storage_kind_toggled(kind: str, enabled: bool) -> str:
    label = buttons.STORAGE_KIND_LABELS.get(kind, kind)
    state = "روشن شد 🟢" if enabled else "خاموش شد 🔴"
    return f"«{label}» {state}"


def storage_all_kinds_toggled(enabled: bool) -> str:
    return (
        "همه\u200cی نوع\u200cها روشن شدن 🟢" if enabled else "همه\u200cی نوع\u200cها خاموش شدن 🔴"
    )


ADMIN_WATCH_PANEL = "<b>رصد اختصاصی</b> 🎯\nهر گروه رو می\u200cتونی توی کانالِ خودش رصد کنی، جدا از ذخیره\u200cسازیِ سراسری.\nبرای هر رصد، نوعِ پیام\u200cها رو جداگانه انتخاب می\u200cکنی."
WATCH_ADD_GROUP_PROMPT = "آیدیِ عددیِ گروهی که می\u200cخوای رصد بشه رو بفرست 👇\n(یه عددِ منفی، مثل <code>-1001234567890</code>)"
WATCH_ADD_CHANNEL_PROMPT = "حالا کانالی که پیام\u200cهای این گروه باید بره توش رو بفرست:\n• یا به شکلِ <code>@username</code>\n• یا دو خط: خط اول لینک، خط دوم آیدیِ عددی"
WATCH_BAD_GROUP = "این آیدیِ گروه نیست 🤔 آیدیِ گروه همیشه منفیه، مثل <code>-1001234567890</code>."
WATCH_EMPTY = "هیچ رصدی ثبت نشده. با «افزودن رصد» شروع کن."
WATCH_GONE = "این رصد دیگه وجود نداره، شاید همین الان حذف شده باشه."
WATCH_REMOVED = "✅ رصد حذف شد؛ دیگه چیزی از این گروه فرستاده نمی\u200cشه."


def watch_added(chat_id: int, group_title: str | None, channel_link: str) -> str:
    where = f" ({html.escape(group_title)})" if group_title else ""
    return f"✅ رصد ثبت شد.\n\n📍 گروه: <code>{chat_id}</code>{where}\n📡 کانال: {html.escape(channel_link)}\n\n⚠️ فعلاً هیچ نوعی روشن نیست، از لیستِ رصدها بازش کن و انتخاب کن چی ذخیره بشه."


def watch_list_header(total: int, page: int, pages: int) -> str:
    return f"<b>رصدهای اختصاصی</b> 🎯 · مجموعاً <b>{pd(total)}</b>\nصفحه {pd(page + 1)} از {pd(pages)}\n\nبرای دیدن و تنظیمِ هر کدوم، روش بزن 👇"


def watch_list_row(chat_id: int, group_title: str | None, kinds_on: int) -> str:
    """One line of the list — a companion to the button, so the text carries the
    ids and counts that would not fit on a button label."""
    name = html.escape(group_title) if group_title else "بدون نام"
    state = f"{pd(kinds_on)} نوع روشن" if kinds_on else "خاموش ⚠️"
    return f"• {name} · <code>{chat_id}</code> · {state}\n"


def watch_button_label(chat_id: int, group_title: str | None) -> str:
    """The button's own label: the name when we have one, else the raw id."""
    if group_title:
        return group_title[:40]
    return str(chat_id)


def watch_detail(
    chat_id: int,
    group_title: str | None,
    channel_link: str,
    channel_id: int,
    enabled: frozenset[str] | set[str] | list[str],
    *,
    member_count: int | None = None,
) -> str:
    lines = [
        f"• {label}: " + ("روشن 🟢" if kind in enabled else "خاموش 🔴")
        for kind, label in buttons.STORAGE_KIND_LABELS.items()
    ]
    group_line = f"📍 گروه: <code>{chat_id}</code>"
    if group_title:
        group_line = f"📍 گروه: {html.escape(group_title)}\n   🆔 <code>{chat_id}</code>"
    if member_count is not None:
        group_line += f"\n   👥 اعضا: {pdn(member_count)}"
    warning = (
        ""
        if any((kind in enabled for kind in buttons.STORAGE_KIND_LABELS))
        else "\n\n⚠️ هیچ نوعی روشن نیست، پس فعلاً هیچی از این گروه فرستاده نمی\u200cشه."
    )
    return (
        f"<b>رصدِ گروه</b> 🎯\n➖➖➖➖➖➖➖➖➖➖\n{group_line}\n📡 کانال: {html.escape(channel_link)}\n   🆔 <code>{channel_id}</code>\n➖➖➖➖➖➖➖➖➖➖\n\n<b>نوعِ پیام\u200cهایی که ذخیره می\u200cشن</b>\n"
        + "\n".join(lines)
        + warning
    )


def watch_delete_confirm(chat_id: int, group_title: str | None) -> str:
    name = f" ({html.escape(group_title)})" if group_title else ""
    return f"⚠️ رصدِ گروه <code>{chat_id}</code>{name} حذف بشه؟\n\nپیام\u200cهایی که تا الان رفتن توی کانال می\u200cمونن، فقط از این به بعد چیزی فرستاده نمی\u200cشه."


ADMIN_GROUPS_PANEL = "<b>مدیریت گروه\u200cها</b> 🗂\n\nاز اینجا می\u200cتونی لیستِ همه\u200cی گروه\u200cها رو با وضعیتشون ببینی، یا از یه گروه خارج شی.\nیکی از گزینه\u200cها رو انتخاب کن 👇"
ADMIN_GROUPS_MEMBERS_PROMPT = "آیدیِ عددیِ گروهی که می\u200cخوای اعضاش رو ببینی رو بفرست 🔢\n(آیدیِ گروه\u200cها معمولاً با <code>-100</code> شروع می\u200cشه)"
ADMIN_GROUPS_MEMBERS_NOT_FOUND = "همچین گروهی توی دیتابیس پیدا نشد 🤔"
ADMIN_GROUPS_INFO_PROMPT = "آیدیِ عددیِ گروهی که می\u200cخوای اطلاعاتش رو ببینی رو بفرست 🔢\n(آیدیِ گروه\u200cها معمولاً با <code>-100</code> شروع می\u200cشه)"
ADMIN_USERS_REFRESH_RUNNING = "در حال بروزرسانیِ نام و یوزرنیمِ کاربرانِ دارای پروفایلِ کامل... ⏳\nاطلاعاتشون رو دسته\u200cای (batch) از تلگرام دوباره می\u200cگیرم؛ برای تعدادِ زیاد ممکنه چند لحظه طول بکشه."
ADMIN_GROUPS_LEAVE_NOT_FOUND = "همچین گروهی توی دیتابیس پیدا نشد 🤔"
ADMIN_GROUPS_LEAVE_RUNNING = "در حال خروج از گروه... 👋"
ADMIN_NUDGE_ADMIN_RUNNING = "در حال فرستادنِ درخواستِ ادمین\u200cشدن به گروه\u200cهای غیرادمین... ⏳"
ADMIN_NUDGE_ADMIN_ASK = "<b>درخواستِ ادمین\u200cشدن از گروه\u200cها</b> 🛡\n\nتوی گروه\u200cهایی که ادمین نیستم، مالک رو تگ می\u200cکنم و می\u200cگم ادمینم کنه.\nبعضی گروه\u200cها ممکنه اجازه\u200cی ارسال پیام بهم ندن.\n\nوقتی تموم شد، لیستِ اون گروه\u200cها رو با اسم برات می\u200cفرستم و اون\u200cجا انتخاب می\u200cکنی از کدوم دسته بیرون بیام و توی کدوم بمونم 👌"
ADMIN_NUDGE_ADMIN_CANCELLED = "باشه، کاری نکردم ✅"
ADMIN_NUDGE_OWNERS_NONE = (
    "هیچ گروهی نداریم که ربات ازش حذف شده باشه و آیدیِ مالک یا مدیراش رو داشته باشیم 🤷"
)
_NUDGE_TARGET_LABELS: Final[dict[str, str]] = {
    "owner": "مالکِ گروه",
    "admins": "مدیرای گروه",
    "both": "مالک و مدیرا",
}


def nudge_target_label(target: str) -> str:
    return _NUDGE_TARGET_LABELS.get(target, "مالکِ گروه")


def admin_nudge_owners_ask_target(count: int, owners: int, admins: int) -> str:
    return f"<b>یادآوری به گروه\u200cهای حذف\u200cکننده</b> 📩\n\n{pd(count)} گروه پیدا شد که ربات ازشون حذف شده و آیدیِ مالک یا مدیراشون رو داریم.\n• مالکِ ذخیره\u200cشده: {pd(owners)} نفر\n• مدیرِ ذخیره\u200cشده: {pd(admins)} نفر\n\nپیامِ یادآوری برای <b>کیا</b> فرستاده بشه؟ 👇"


def admin_nudge_owners_ask_name(target: str) -> str:
    return f"گیرنده: <b>{nudge_target_label(target)}</b> ✅\n\nمی\u200cخوای توی پیام <b>اسمِ گروه</b> هم ذکر بشه؟ 👇"


ADMIN_NUDGE_OWNERS_RUNNING = "در حال فرستادنِ یادآوری به گروه\u200cهای حذف\u200cکننده... ⏳"


def admin_nudge_owners_done(sent: int, failed: int, target: str) -> str:
    return f"تموم شد ✅\n• گیرنده: {nudge_target_label(target)}\n• یادآوری فرستاده شد به: {pd(sent)} نفر\n• ناموفق (استارت\u200cنزده/بلاک): {pd(failed)}"


ADMIN_IMPORT_MEMBERS_RUNNING = "در حال واردکردنِ اعضای گروه\u200cهایی که ادمینشونم... ⏳\n(لیستِ اعضا + پیامای اخیرِ هر گروه خونده می\u200cشه)"
ADMIN_IMPORT_MEMBERS_NONE = "هیچ گروهِ فعالی که توش ادمین باشم پیدا نشد 🤔\nاول از «بررسی سلامت گروه\u200cها» وضعیت رو تازه کن."
ADMIN_IMPORT_MEMBERS_CANCELLED = "لغو شد ✅ هیچ عضوی وارد نشد."


def admin_import_members_confirm(admin_groups: int, active_groups: int) -> str:
    """Render the reference administrator view."""
    return f"<b>واردکردنِ اعضای گروه\u200cها</b> 👥\n\n• گروهِ فعال: {pd(active_groups)}\n• از این\u200cها ادمین هستم (اجرا می\u200cشه روشون): <b>{pd(admin_groups)}</b>\n\n<b>چی\u200cکار می\u200cکنه؟</b>\nبرای هر گروه، لیستِ اعضا و پیامای اخیرش رو می\u200cخونه و اعضا رو توی دیتابیس اضافه/بروز می\u200cکنه.\n\n<b>چی دست\u200cنخورده می\u200cمونه؟</b>\n• هیچی حذف نمی\u200cشه، فقط افزودن/بروزرسانی\n• «آخرین فعالیت»ِ واقعیِ اعضا بازنویسی نمی\u200cشه\n\n⚠️ برای گروه\u200cهای زیاد ممکنه چند دقیقه طول بکشه (به\u200cخاطرِ محدودیتِ تلگرام بینشون مکث می\u200cکنم). مطمئنی؟"


def admin_import_members_done(groups: int, imported: int, empty: int) -> str:
    return f"تموم شد ✅\n• گروهِ بررسی\u200cشده: {pd(groups)}\n• عضوِ واردشده/بروزشده: {pd(imported)}\n• گروهِ بدونِ نتیجه: {pd(empty)}\n\n<i>گروهِ بدونِ نتیجه یعنی ربات نتونست لیستِ اعضا رو بخونه؛ معمولاً چون دسترسیِ ادمینش کامل نیست.</i>"


def promo_panel(*, interval_hours: int, enabled: bool) -> str:
    state = "فعاله 🟢" if enabled else "خاموشه 🔴"
    return f"<b>پیام تبلیغیِ گروه\u200cها</b> 📣\n\nوضعیت: <b>{state}</b>\nبازه\u200cی ارسال: هر <b>{pd(interval_hours)}</b> ساعت یه\u200cبار\n\nهر چند ساعت یه\u200cبار توی گروه\u200cها یه پیامِ تصادفی می\u200cذارم (گروه\u200cهای ادمین: دعوت به استفاده + /help • گروه\u200cهای بدونِ ادمینی: درخواستِ ادمین\u200cشدن).\n\n• با دکمه\u200cی بالا روشن/خاموشش کن\n• یا یه عدد (ساعت) از پایین انتخاب کن تا بازه عوض شه"


def promo_interval_set(hours: int) -> str:
    return f"✅ از این به بعد هر {pd(hours)} ساعت یه\u200cبار پیامِ تبلیغی می\u200cذارم."


def promo_toggled(enabled: bool) -> str:
    return "✅ پیامِ تبلیغی روشن شد 🟢" if enabled else "✅ پیامِ تبلیغی خاموش شد 🔴"


def admin_nudge_panel(*, interval_hours: int, enabled: bool) -> str:
    state = "فعاله 🟢" if enabled else "خاموشه 🔴"
    return f"<b>یادآوریِ خودکارِ ادمین\u200cشدن</b> 🛡\n\nوضعیت: <b>{state}</b>\nبازه\u200cی ارسال: هر <b>{pd(interval_hours)}</b> ساعت یه\u200cبار\n\nهر چند ساعت یه\u200cبار، توی گروه\u200cهایی که هنوز ادمین نیستم مالکِ گروه رو تگ می\u200cکنم و می\u200cگم ادمینم کنه. اگه ببینم جایی ادمین شده\u200cم ولی توی دیتابیس ثبت نشده، همون\u200cجا ثبتش می\u200cکنم و اعلام می\u200cکنم.\n\n• با دکمه\u200cی بالا روشن/خاموشش کن\n• یا یه عدد (ساعت) از پایین انتخاب کن تا بازه عوض شه"


def admin_nudge_interval_set(hours: int) -> str:
    return f"✅ از این به بعد هر {pd(hours)} ساعت یه\u200cبار یادآوریِ ادمین\u200cشدن می\u200cفرستم."


def admin_nudge_toggled(enabled: bool) -> str:
    return (
        "✅ یادآوریِ خودکارِ ادمین\u200cشدن روشن شد 🟢"
        if enabled
        else "✅ یادآوریِ خودکارِ ادمین\u200cشدن خاموش شد 🔴"
    )


DEFAULT_DISPLAY_NAME = "کاربر"


def mention(user_id: int, name: str | None) -> str:
    """Render the reference administrator view."""
    safe = html.escape(name or DEFAULT_DISPLAY_NAME)
    rli, pdi = ("\u2067", "\u2069")
    return f'{rli}<a href="tg://user?id={user_id}">{safe}</a>{pdi}'


_PAGE_DIVIDER = "➖➖➖➖➖➖➖➖➖➖"


def _page_header(title: str, *, total: int, unit: str, page: int, total_pages: int) -> str:
    """Render the reference administrator view."""
    return f"{title}\n📄 مجموع: <b>{pd(total)}</b> {unit} · صفحه <b>{pd(page + 1)}</b> از {pd(total_pages)}"


def _paged(header: str, rows: list[str]) -> str:
    """Join a header + rows with the shared divider (header on top)."""
    return f"\n{_PAGE_DIVIDER}\n".join([header, *rows]) if rows else header


def _ltr(text: str) -> str:
    """Render the reference administrator view."""
    return f"\u2066{text}\u2069"


def _yn(value: bool | None) -> str:
    if value is None:
        return "نامشخص"
    return "بله ✅" if value else "خیر ❌"


_GROUP_DIVIDER = _PAGE_DIVIDER
_GROUPS_FILTER_FA = {"all": "همه", "in": "عضو", "out": "خارج\u200cشده"}
_GROUPS_ORDER_FA = {"date": "جدیدترین", "mem": "بیشترین عضو", "mem_asc": "کمترین عضو"}


def groups_list_page(
    items: list[dict],
    page: int,
    total_pages: int,
    total: int,
    flt: str = "all",
    order: str = "date",
) -> str:
    header = (
        _page_header(
            "<b>لیستِ گروه\u200cها</b> 🗂",
            total=total,
            unit="گروه",
            page=page,
            total_pages=total_pages,
        )
        + f"\n🔎 فیلتر: {_GROUPS_FILTER_FA.get(flt, flt)} • ترتیب: {_GROUPS_ORDER_FA.get(order, order)}\n<i>از دیتابیس (آخرین «بررسیِ سلامت»)؛ برای تازه\u200cکردنِ اعداد، سلامت\u200cسنجی رو اجرا کن</i>"
    )
    if not items:
        return header + f"\n{_PAGE_DIVIDER}\nهیچ گروهی با این فیلتر پیدا نشد."
    blocks: list[str] = [header]
    blocks.extend((group_block(it) for it in items))
    return f"\n{_GROUP_DIVIDER}\n".join(blocks)


def group_block(it: dict) -> str:
    """One group's details, as the list renders them.

    Shared with :func:`group_info_page` so the single-group view cannot
    drift from the list it is meant to mirror — the admin reads the same
    fields in the same order either way.
    """
    title = html.escape(it["title"]) if it.get("title") else "بدونِ نام"
    member = it["member"]
    lines = [
        f"🔹 <b>{title}</b>",
        f"  🆔 <code>{it['chat_id']}</code>",
        f"  📌 وضعیت: {('نامشخص' if member is None else 'عضوم ✅' if member else 'خارج\u200cشده/حذف ❌')}",
    ]
    if member:
        lines.append(f"  🛡 ادمین: {_yn(it['is_admin'])}")
        lines.append(f"  💬 ارسال پیام: {_yn(it['can_send'])}")
        members = it.get("members")
        if members is not None:
            lines.append(f"  👥 تعداد اعضا: {members}")
    link = it.get("link")
    if link:
        safe = html.escape(link)
        lines.append(f'  🔗 <a href="{safe}">لینکِ عضویت</a>')
    elif member and it.get("can_invite") is False:
        lines.append("  🔗 لینک: ندارم، دسترسیِ «دعوت کاربر با لینک» رو بده ⚠️")
    return "\n".join(lines)


def group_info_page(it: dict) -> str:
    """One group's card, looked up by id — the list's entry for that
    group under its own header, without the list's pagination furniture.
    """
    return "\n".join(["<b>اطلاعاتِ گروه</b> 🗂", _PAGE_DIVIDER, group_block(it)])


_MEMBERS_ACTIVE_FA = {"all": "همه", "in": "عضو", "out": "خارج\u200cشده"}
_MEMBERS_STARTED_FA = {"all": "همه", "yes": "استارت\u200cزده", "no": "استارت\u200cنزده"}


def group_members_page(
    items: list,
    page: int,
    total_pages: int,
    total: int,
    chat_id: int,
    group_title: str | None,
    *,
    active: str = "all",
    started: str = "all",
) -> str:
    """One page of ``chat_id``'s membership roster — mirrors
    :func:`groups_list_page`'s shape, one group's members instead of the
    group list itself. ``items`` are ``schemas.membership.MemberRowDTO``."""
    title = html.escape(group_title) if group_title else "بدونِ نام"
    header = (
        _page_header(
            f"<b>اعضای «{title}»</b> 👥",
            total=total,
            unit="عضو",
            page=page,
            total_pages=total_pages,
        )
        + f"\n🆔 <code>{chat_id}</code>\n🔎 وضعیت: {_MEMBERS_ACTIVE_FA.get(active, active)} • استارت: {_MEMBERS_STARTED_FA.get(started, started)}"
    )
    if not items:
        return header + f"\n{_PAGE_DIVIDER}\nهیچ عضوی با این فیلتر پیدا نشد."
    rows: list[str] = []
    for row in items:
        display = row.first_name
        lines = [
            f"🔹 {mention(row.user_id, display)}",
            f"  🆔 <code>{row.user_id}</code>",
            f"  📌 وضعیت: {('عضو ✅' if row.is_active else 'خارج\u200cشده ❌')}",
            f"  🤖 استارت: {_yn(row.is_started)}",
            f"  🕒 آخرین حضور: {_ltr(to_shamsi(row.last_seen_at))}",
        ]
        if row.username:
            lines.append(f"  🔗 @{html.escape(row.username)}")
        rows.append("\n".join(lines))
    return _paged(header, rows)


def group_card(group):
    return {
        "chat_id": group.chat_id,
        "title": group.title,
        "member": group.is_active,
        "is_admin": group.is_admin,
        "can_send": group.can_send,
        "members": group.member_count,
        "link": group.link,
    }
