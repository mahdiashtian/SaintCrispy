"""Site-specific menu and file copy; no extraction or network behavior belongs here."""

from dataclasses import dataclass

from bot.deeplinks import bot_handle
from schemas.media import Media, Quality


@dataclass(frozen=True)
class DownloadCopy:
    icon: str
    name: str
    tagline: str
    title_label: str | None
    creator_label: str
    prompt: str


DOWNLOAD_COPY = {
    "soundcloud": DownloadCopy(
        "🎵",
        "SoundCloud",
        "آهنگ دلخواهت، آمادهٔ شنیدن",
        "🎧 آهنگ",
        "👤 هنرمند",
        "کیفیت آهنگ رو انتخاب کن:",
    ),
    "youtube": DownloadCopy(
        "▶️",
        "YouTube",
        "ویدیوی دلخواهت، همراه همیشگی تو",
        "🎬 عنوان",
        "👤 کانال",
        "کیفیت ویدیو یا نسخهٔ صوتی رو انتخاب کن:",
    ),
    "instagram": DownloadCopy(
        "📸",
        "Instagram",
        "لحظه‌های دیدنی رو پیش خودت نگه دار",
        None,
        "👤 صفحه",
        "کیفیت پست یا بخش آلبوم رو انتخاب کن:",
    ),
    "pinterest": DownloadCopy(
        "📌",
        "Pinterest",
        "ایده‌های دوست‌داشتنی، آمادهٔ ذخیره",
        "📎 عنوان پین",
        "👤 منتشرکننده",
        "نسخهٔ عکس یا ویدیو رو انتخاب کن:",
    ),
    "xvideos": DownloadCopy(
        "🎬",
        "XVideos",
        "ویدیوی انتخابی تو، با کیفیت دلخواهت",
        "🎞️ عنوان",
        "👤 منتشرکننده",
        "کیفیت ویدیو رو انتخاب کن:",
    ),
    "xnxx": DownloadCopy(
        "📽️",
        "XNXX",
        "ویدیوی دلخواهت رو برای بعد نگه دار",
        "🎞️ عنوان",
        "👤 منتشرکننده",
        "نسخهٔ دلخواهت رو برای دانلود انتخاب کن:",
    ),
}
DEFAULT_COPY = DownloadCopy(
    "📥",
    "دانلود رسانه",
    "رسانهٔ دلخواهت، همین‌جا",
    "📎 عنوان",
    "👤 سازنده",
    "کیفیت دلخواهت رو انتخاب کن:",
)


def _field(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    return encoded[: (limit - 1) * 2].decode("utf-16-le", errors="ignore").rstrip() + "…"


def _details(media: Media, copy: DownloadCopy) -> list[str]:
    lines = []
    if copy.title_label and (title := _field(media.title, 400)):
        lines.append(f"{copy.title_label}: {title}")
    if creator := _field(media.artist, 100):
        if media.site == "instagram" and creator != "Instagram":
            creator = "@" + creator.lstrip("@")
        lines.append(f"{copy.creator_label}: {creator}")
    return lines


def quality_menu_text(media: Media) -> str:
    copy = DOWNLOAD_COPY.get(media.site, DEFAULT_COPY)
    details = "\n".join(_details(media, copy))
    return f"{copy.icon} {copy.name}\n\n{details}\n\n{copy.prompt}"


def inspection_text(site: str) -> str:
    copy = DOWNLOAD_COPY.get(site, DEFAULT_COPY)
    return f"{copy.icon} {copy.name}\n\n⏳ دارم فرمت‌ها و کیفیت‌های قابل دانلود رو می‌گیرم…"


def download_caption(media: Media, quality: Quality, bot_username: str | None = None) -> str:
    copy = DOWNLOAD_COPY.get(media.site, DEFAULT_COPY)
    details = _details(media, copy)
    details.append(f"⚙️ کیفیت: {_field(quality.label, 120)}")
    return (
        f"{copy.icon} {copy.name}\n{copy.tagline}\n\n"
        + "\n".join(details)
        + f"\n\n💬 دریافت با {bot_handle(bot_username)}"
    )
