"""User-facing copy, separate from business and storage code."""


def start_text(bot_username: str | None, interval: int = 60) -> str:
    handle = f"@{bot_username}" if bot_username else "این ربات"
    return (
        f"📬 به {handle} خوش اومدی!\n\n"
        "لینک را بفرست، کیفیت را انتخاب کن و فایل را همین‌جا بگیر. 📥\n"
        "برای دریافت فایل نیازی به زدن /start نیست.\n\n"
        "📋 سایت‌های پشتیبانی‌شده:\n"
        "🎵 SoundCloud\n▶️ YouTube\n📸 Instagram\n📌 Pinterest\n🎬 XVideos\n🎬 XNXX\n\n"
        f"⏱ هر {interval} ثانیه یک لینک می‌تونی بفرستی.\n"
        "⏹ برای لغو دریافت، دکمهٔ توقف زیر پیام وضعیت را بزن.\n"
        "♻️ فایل‌های قبلی از تلگرام دوباره ارسال می‌شوند.\n\n"
        f"💬 ربات: {handle}"
    )
