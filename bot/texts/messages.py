"""User-facing copy, separate from business and storage code."""

from bot.deeplinks import bot_handle


def start_text(bot_username: str | None, interval: int = 60) -> str:
    handle = bot_handle(bot_username)
    return (
        f"📬 به {handle} خوش اومدی!\n\n"
        "لینک رو بفرست، کیفیت رو انتخاب کن و فایل رو همین‌جا بگیر.\n"
        "لازم نیست قبل از هر لینک /start بزنی.\n\n"
        "📋 سایت‌های پشتیبانی‌شده:\n"
        "🎵 SoundCloud\n▶️ YouTube\n📸 Instagram\n📌 Pinterest\n🎬 XVideos\n🎬 XNXX\n\n"
        f"⏱ هر {interval} ثانیه یک لینک می‌تونی بفرستی.\n"
        "برای لغو دانلود، «توقف» زیر پیام وضعیت رو بزن.\n"
        "فایلی که قبلاً گرفته باشم، از تلگرام دوباره برات می‌فرستم.\n\n"
        f"💬 ربات: {handle}"
    )


def help_text(interval: int = 60) -> str:
    return (
        "📖 راهنمای دانلود\n\n"
        "لینک آهنگ، ویدیو یا پست رو بفرست. بعد یکی از کیفیت‌ها رو بزن.\n"
        "کیفیت‌های بالاتر در ردیف اول دکمه‌ها هستن. "
        "برای آلبوم اینستاگرام، هر بخش دکمه‌های خودش رو داره.\n\n"
        f"بین دو درخواست {interval} ثانیه فاصله بذار. "
        "برای لغو دانلود، «توقف» زیر پیام وضعیت رو بزن.\n"
        "اگه دکمهٔ کیفیت منقضی شد، لینک رو دوباره بفرست."
    )


def sites_text() -> str:
    return (
        "🌐 لینک چی رو می‌تونم بگیرم؟\n\n"
        "▶️ YouTube: ویدیو، شورت و فایل صوتی\n"
        "📸 Instagram: پست، ریلز و بخش‌های آلبوم؛ استوری با نشست مجاز\n"
        "🎵 SoundCloud: آهنگ و کیفیت‌های موجود\n"
        "📌 Pinterest: عکس و ویدیو\n"
        "🎬 XVideos و XNXX: ویدیو\n\n"
        "محتوای خصوصی یا محدود ممکنه به حساب دارای دسترسی نیاز داشته باشه."
    )
