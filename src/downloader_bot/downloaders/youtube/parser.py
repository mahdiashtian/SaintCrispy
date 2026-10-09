import math
import re
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

from downloader_bot.models import DownloadError, Media, Quality, Source

from .urls import PAGE_HOSTS as HOSTS

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


def video_id(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.port is not None:
            raise ValueError
        if parsed.username or parsed.password:
            raise ValueError
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname in {"youtu.be", "www.youtu.be"} and len(parts) == 1:
            identity = parts[0]
        elif parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            identity = values[0] if len(values) == 1 else ""
        elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            identity = parts[1]
        else:
            raise ValueError
        if not VIDEO_ID.fullmatch(identity):
            raise ValueError
        return identity
    except ValueError:
        raise DownloadError(
            "لینک یک ویدیوی یوتیوب را بفرست؛ صفحه کانال و پلی‌لیست پشتیبانی نمی‌شود."
        ) from None


def canonical_url(identity: str) -> str:
    return f"https://www.youtube.com/watch?v={identity}"


def number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0


def usable(item: dict) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("format_id"), str)
        and bool(item["format_id"])
        and isinstance(item.get("url"), str)
        and item["url"].startswith("https://")
        and item.get("protocol") in {"https", "http", "m3u8", "m3u8_native"}
        and not item.get("has_drm")
    )


def codec(item: dict) -> str:
    video = item.get("vcodec") or "none"
    if video != "none":
        if video.startswith(("avc1", "h264")):
            return "h264"
        if video.startswith(("av01", "av1")):
            return "av1"
        if video.startswith(("vp9", "vp09")):
            return "vp9"
        return ""
    audio = item.get("acodec") or "none"
    if audio.startswith(("mp4a", "aac")):
        return "aac"
    if audio.startswith("opus"):
        return "opus"
    return ""


def is_hls(item: dict) -> bool:
    return item.get("protocol") in {"m3u8", "m3u8_native"}


@dataclass(frozen=True)
class Selection:
    quality: Quality
    video: dict = field(repr=False)
    audio: dict | None = field(default=None, repr=False)

    def source(self, proxy: str, duration: float = 0) -> Source:
        return Source(
            self.video["url"],
            self.quality.protocol,
            source_headers(self.video),
            self.audio["url"] if self.audio else None,
            audio_headers=source_headers(self.audio) if self.audio else {},
            proxy=proxy,
            size_bytes=int(number(self.video.get("filesize"))) or None,
            chunk_size=2 * 1024 * 1024 if self.quality.protocol == "progressive" else None,
            require_audio=self.quality.mime_type.startswith("video/"),
            duration=duration or None,
            input_protocol="hls" if is_hls(self.video) else "progressive",
            audio_protocol=("hls" if is_hls(self.audio) else "progressive") if self.audio else None,
        )


def source_headers(item: dict) -> dict[str, str]:
    headers = dict(item.get("http_headers") or {})
    # yt-dlp serializes scoped cookies separately from http_headers.
    jar = SimpleCookie()
    jar.load(item.get("cookies") or "")
    url = urlsplit(item["url"])
    cookies = []
    for value in jar.values():
        domain = value["domain"].lstrip(".").lower()
        path = value["path"] or "/"
        if domain and (url.hostname == domain or url.hostname.endswith("." + domain)):
            if url.path.startswith(path):
                cookies.append(f"{value.key}={value.coded_value}")
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    return headers


def selections(data: dict, *, include_alternatives: bool = False) -> tuple[Selection, ...]:
    formats = [item for item in (data.get("formats") or []) if usable(item) and codec(item)]
    audios = [item for item in formats if item.get("vcodec") == "none"]

    def best_audio(family: str):
        candidates = [item for item in audios if codec(item) == family]
        return max(
            candidates,
            key=lambda item: (
                number(item.get("language_preference")),
                number(item.get("quality")),
                number(item.get("abr") or item.get("tbr")),
                not is_hls(item),
            ),
            default=None,
        )

    chosen, ranks, alternatives = {}, {}, []
    for item in formats:
        family = codec(item)
        if family not in {"h264", "av1", "vp9"}:
            continue
        height, width = int(number(item.get("height"))), int(number(item.get("width")))
        if not height or not width:
            continue
        fps = round(number(item.get("fps"))) or 30
        has_audio = item.get("acodec") not in {None, "none"}
        audio = None if has_audio else best_audio("opus" if family == "vp9" else "aac")
        if not has_audio and audio is None:
            continue  # A silent video is not a complete download.
        extension = "webm" if family == "vp9" else "mp4"
        protocol = (
            "hls"
            if is_hls(item) or (audio and is_hls(audio))
            else ("dash" if audio else "progressive")
        )
        key = f"video_{height}p{fps}_{family}_{extension}"
        label = f"{height}p" + (f" · {fps}fps" if fps > 30 else "")
        dynamic_range = item.get("dynamic_range") or "SDR"
        if dynamic_range != "SDR":
            key += "_" + re.sub(r"[^a-z0-9]", "_", dynamic_range.lower())
            label += f" · {dynamic_range}"
        if "Premium" in (item.get("format_note") or ""):
            key += "_premium"
            label += " · Premium"
        label += f" · {family.upper()} · {extension.upper()}"
        rate = number(item.get("tbr")) + (number(audio.get("abr")) if audio else 0)
        quality = Quality(
            key,
            label,
            family,
            round(rate) or None,
            extension,
            f"video/{extension}",
            protocol,
            item["format_id"],
            width=width,
            height=height,
        )
        alternatives.append(Selection(quality, item, audio))
        rank = (has_audio and not is_hls(item), not is_hls(item), rate)
        if key not in ranks or rank > ranks[key]:
            ranks[key] = rank
            chosen[key] = Selection(quality, item, audio)
    videos = sorted(
        alternatives if include_alternatives else chosen.values(),
        key=lambda item: (
            -(item.quality.height or 0),
            -number(item.video.get("fps")),
            {"h264": 0, "av1": 1, "vp9": 2}[item.quality.codec],
        ),
    )
    for family in ("aac", "opus"):
        preferred = best_audio(family)
        if preferred is None:
            continue
        # Keep every native quality of the chosen language, including low bitrates.
        # A higher-bitrate dub must not replace the original audio of the video.
        originals = [
            item
            for item in audios
            if (codec(item) == family and item.get("language") == preferred.get("language"))
        ]
        audio_choices, audio_alternatives = {}, []
        for item in originals:
            extension = "m4a" if family == "aac" else "webm"
            bitrate = round(number(item.get("abr") or item.get("tbr"))) or None
            language = re.sub(r"[^A-Za-z0-9_-]", "_", item.get("language") or "und")
            key = f"audio_{item['format_id']}_{language}_{family}"
            label = f"صوت {family.upper()}" + (f" · {bitrate} kbps" if bitrate else "")
            channels = int(number(item.get("audio_channels")))
            if channels > 2:
                label += f" · {channels}ch"
            quality = Quality(
                key,
                label,
                family,
                bitrate,
                extension,
                "audio/mp4" if family == "aac" else "audio/webm",
                "hls" if is_hls(item) else "progressive",
                item["format_id"],
            )
            audio_alternatives.append(Selection(quality, item))
            previous = audio_choices.get(key)
            if previous is None or (not is_hls(item), number(item.get("tbr"))) > (
                not is_hls(previous.video),
                number(previous.video.get("tbr")),
            ):
                audio_choices[key] = Selection(quality, item)
        videos.extend(
            sorted(
                audio_alternatives if include_alternatives else audio_choices.values(),
                key=lambda choice: (
                    -(choice.quality.bitrate or 0),
                    choice.quality.key,
                ),
            )
        )
    return tuple(videos)


def read_media(data: dict, identity: str) -> Media:
    if data.get("id") != identity or data.get("_type", "video") != "video":
        raise DownloadError("شناسه پاسخ یوتیوب با ویدیوی درخواستی یکسان نیست.")
    if data.get("is_live") or data.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise DownloadError("پخش زنده یا ویدیوی در حال آماده‌سازی هنوز پشتیبانی نمی‌شود.")
    choices = selections(data)
    if not choices:
        raise DownloadError(
            "کیفیت کامل و بدون DRM برای این ویدیو پیدا نشد.", code="youtube_no_complete_formats"
        )
    return Media(
        "youtube",
        identity,
        data.get("title") or identity,
        data.get("uploader") or data.get("channel") or "YouTube",
        math.ceil(number(data.get("duration"))),
        canonical_url(identity),
        data.get("thumbnail"),
        tuple(item.quality for item in choices),
    )
