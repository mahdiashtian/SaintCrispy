"""Private administration copy, independent of storage and Telegram adapters."""

HOME = "🛠 پنل مدیریت\nآمار، کاربران، گروه‌ها و عملیات پایدار ربات را از این بخش مدیریت کن."
DENIED = "⛔️ دسترسی مدیریت مجاز نیست."
PROMPTS = {
    "user_quota": "حداکثر درخواست هر کاربر در دقیقه را از ۱ تا ۱۰۰۰ بفرست.",
    "group_quota": "سقف پیش‌فرض لینک هر گروه در دقیقه را از ۱ تا ۱۰۰۰ بفرست.",
    "import_users": "📥 فایل CSV با حداکثر ۵۰۰ ردیف و یک مگابایت را در پاسخ به این پیام بفرست. ستون‌ها: user_id,username,first_name,last_name. نقش مدیر، استارت و عضویت از فایل پذیرفته نمی‌شود.",
    "lookup": "🔎 شناسه عددی یا نام کاربری را در پاسخ به همین پیام بفرست.",
    "group_lookup": "👥 شناسه منفی گروه را در پاسخ به همین پیام بفرست.",
    "grant": "👤 شناسه عددی مدیر جدید را در پاسخ به همین پیام بفرست.",
    "revoke": "👤 شناسه عددی مدیری که دسترسی‌اش حذف شود را بفرست.",
    "ban": "⛔️ شناسه عددی حسابی که دریافت فایل برایش مسدود شود را بفرست.",
    "unban": "✅ شناسه عددی حسابی که رفع مسدودی شود را بفرست.",
    "limit": "⏱ شناسه منفی گروه و سقف لینک در دقیقه را با فاصله بفرست؛ مثلاً: ‎-12345 5",
    "broadcast": "📨 متن ارسال را در پاسخ به همین پیام بفرست. برای استثنا، خط اول را به شکل «استثنا: -123,-456» بنویس. پیش از شروع، تأیید جدا می‌گیری.",
    "forward": "↪️ پیام دلخواه را در پاسخ به همین پیام بفرست یا فوروارد کن. پیش از شروع، مخاطبان را تأیید می‌کنی.",
}


def flag(value):
    return "نامعلوم" if value is None else "بله" if value else "خیر"


def group_text(group):
    return (
        f"👥 {group.title or 'گروه بدون عنوان'}\nشناسه: {group.chat_id}\n"
        f"عضو گروه: {flag(group.is_active)} · ادمین: {flag(group.is_admin)}\n"
        f"امکان ارسال: {flag(group.can_send)}\nآورنده اول: {group.added_by_user_id or 'نامعلوم'}\n"
        f"مالک: {group.owner_id or 'نامعلوم'}\nتعداد اعضا: {group.member_count if group.member_count is not None else 'نامعلوم'}\n"
        f"سقف لینک در دقیقه: {group.links_per_minute or 'پیش‌فرض نصب'}\n"
        f"اولین ورود: {group.added_at or 'نامعلوم'}\nآخرین بازگشت: {group.readded_at or 'ندارد'}\n"
        f"آخرین خروج: {group.removed_at or 'ندارد'}\nشروع ادمینی: {group.admin_since or 'ندارد'}\n"
        f"آخرین فعالیت: {group.last_activity_at or 'ثبت نشده'}\n"
        f"مقصد مهاجرت: {group.migrated_to or 'ندارد'}"
    )


def user_text(user):
    return (
        f"👤 {user.first_name or 'کاربر'}\nشناسه: {user.user_id}\nنام کاربری: @{user.username or '—'}\n"
        f"استارت خصوصی: {flag(user.is_started)} · مدیر: {flag(user.is_admin)}\n"
        f"مسدود در ربات: {flag(user.is_banned)} · بلاک‌کردن ربات: {flag(user.is_blocked)}\n"
        f"نوع حساب: {user.account_kind}\nآخرین فعالیت: {user.last_seen_at}"
    )
