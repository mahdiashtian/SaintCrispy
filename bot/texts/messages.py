"""User-facing copy, separate from business and storage code."""

from bot.deeplinks import bot_handle


def start_text(
    bot_username: str | None, interval: int = 60, *, youtube_enabled=False, user_limit=None
) -> str:
    handle = bot_handle(bot_username)
    youtube = "▶️ YouTube\n" if youtube_enabled else ""
    quota = (
        f"⏱ سقف هر کاربر: {user_limit} درخواست در دقیقه.\n"
        if user_limit is not None
        else f"⏱ هر {interval} ثانیه یک لینک می‌تونی بفرستی.\n"
    )
    return (
        f"📬 به {handle} خوش اومدی!\n\n"
        "لینک رو بفرست، کیفیت رو انتخاب کن و فایل رو همین‌جا بگیر.\n\n"
        "📋 سایت‌های پشتیبانی‌شده:\n"
        f"🎵 SoundCloud\n🟢 Spotify\n{youtube}📸 Instagram\n🎵 TikTok\n📌 Pinterest\n🎬 XVideos\n🎬 XNXX\n\n"
        f"{quota}"
        "برای لغو دانلود، «توقف» زیر پیام وضعیت رو بزن.\n"
        "فایلی که قبلاً گرفته باشم، از تلگرام دوباره برات می‌فرستم.\n\n"
        f"💬 ربات: {handle}"
    )


def help_text(interval: int = 60, group_limit: int = 5, *, user_limit=None) -> str:
    quota = (
        f"سقف هر کاربر {user_limit} درخواست در دقیقه است. "
        if user_limit is not None
        else f"بین دو درخواست {interval} ثانیه فاصله بذار. "
    )
    return (
        "📖 راهنمای دانلود\n\n"
        "لینک آهنگ، ویدیو یا پست رو بفرست. بعد یکی از کیفیت‌ها رو بزن.\n"
        "کیفیت‌های بالاتر در ردیف اول دکمه‌ها هستن. "
        "برای آلبوم اینستاگرام، هر بخش دکمه‌های خودش رو داره.\n\n"
        f"{quota}"
        "برای لغو دانلود، «توقف» زیر پیام وضعیت رو بزن.\n"
        "دکمه‌های کیفیت فقط برای فرستندهٔ همان لینک کار می‌کنن.\n"
        "اگه دکمهٔ کیفیت منقضی شد، لینک رو دوباره بفرست.\n\n"
        f"👥 سقف لینک‌های هر گروه: {group_limit} لینک در دقیقه.\n"
        "این راهنما با /help یا «📖 راهنما» در دسترسه.\n\n"
        "👇 هر موضوعی رو بزن تا توضیحش رو ببینی:"
    )


def sites_text(*, youtube_enabled=False) -> str:
    youtube = "▶️ YouTube: ویدیو، شورت و فایل صوتی\n" if youtube_enabled else ""
    return (
        "🌐 لینک چی رو می‌تونم بگیرم؟\n\n"
        f"{youtube}📸 Instagram: پست، ریلز و بخش‌های آلبوم؛ استوری با نشست مجاز\n"
        "🎵 SoundCloud: آهنگ و کیفیت‌های موجود\n"
        "🟢 Spotify: لینک آهنگ؛ MP3 نسخهٔ متناظر از منبع جایگزین، نه صوت اصلی اسپاتیفای\n"
        "🎵 TikTok: ویدیو، عکس‌های پست و موسیقی\n"
        "📌 Pinterest: عکس و ویدیو\n"
        "🎬 XVideos و XNXX: ویدیو\n\n"
        "محتوای خصوصی یا محدود ممکنه به حساب دارای دسترسی نیاز داشته باشه."
    )
