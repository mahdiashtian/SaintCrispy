"""Parse TikTok web state and TikWM-compatible metadata without network I/O."""

import hashlib
import json
import math
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

from schemas.media import DownloadError, Media, Quality, Source

from .urls import canonical_url, content_id, media_url


@dataclass(frozen=True)
class Catalog:
    media: Media
    sources: dict[str, Source] = field(repr=False)


def number(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return int(result) if math.isfinite(result) and 0 < result < 2**63 else None
    except (TypeError, ValueError, OverflowError):
        return None


def seconds(value) -> int:
    return number(value) or 0


def first_url(value) -> str | None:
    if isinstance(value, str):
        return media_url(value)
    if isinstance(value, dict):
        value = value.get("UrlList") or value.get("urlList") or value.get("url_list") or []
    if isinstance(value, list):
        for item in value[:8]:
            url = (
                first_url(item.get("src"))
                if isinstance(item, dict) and "src" in item
                else first_url(item)
            )
            if url:
                return url
    return None


def checked_identity(identity, requested_url) -> str:
    identity = str(identity or "")
    expected = content_id(requested_url)
    canonical_url(identity, "_")
    if expected is not None and identity != expected:
        raise DownloadError(
            "شناسهٔ پاسخ با پست درخواستی یکسان نیست.", code="tiktok_identity_mismatch"
        )
    return identity


def read_resolver(payload: dict, requested_url: str) -> Catalog:
    if (
        not isinstance(payload, dict)
        or type(payload.get("code")) is not int
        or payload["code"] != 0
    ):
        raise DownloadError("تیک‌تاک فایل این پست را برنگرداند.", code="tiktok_resolver_rejected")
    node = payload.get("data")
    if not isinstance(node, dict):
        raise DownloadError("پاسخ تیک‌تاک ناقص است.", code="tiktok_invalid_response")
    identity = checked_identity(node.get("id") or node.get("aweme_id"), requested_url)
    author = node.get("author") if isinstance(node.get("author"), dict) else {}
    username = str(author.get("unique_id") or "_")
    images = node.get("images") if isinstance(node.get("images"), list) else []
    page = canonical_url(identity, username, photo=bool(images))
    duration = seconds(node.get("duration"))
    formats = []
    sources = {}

    def add(key, label, value, mime, extension, size=None, item_duration=None):
        url = media_url(value)
        if not url:
            return
        size = number(size)
        if size:
            label += f" · {size / 1048576:.1f} MB"
        quality = Quality(
            key,
            label,
            "video" if mime.startswith("video") else extension,
            None,
            extension,
            mime,
            "progressive",
            url,
            duration=item_duration,
        )
        formats.append(quality)
        sources[key] = Source(
            url,
            "progressive",
            size_bytes=size,
            duration=item_duration or None,
            require_audio=mime.startswith("video") and bool(node.get("music")),
        )

    if isinstance(images, list) and images:
        if len(images) > 80:
            raise DownloadError(
                "تعداد عکس‌های این پست بیش از سقف پشتیبانی است.", code="tiktok_too_many_images"
            )
        for index, image in enumerate(images, 1):
            url = media_url(image)
            if not url:
                continue
            # Stable asset paths survive signed-query renewal and carousel reordering.
            asset = hashlib.sha256(urlsplit(url).path.encode()).hexdigest()[:16]
            key = "photo_" + asset
            if key not in sources:
                add(key, f"عکس {index}", url, "image/jpeg", "jpg")
    else:
        add(
            "video_hd",
            "ویدیو HD",
            node.get("hdplay"),
            "video/mp4",
            "mp4",
            node.get("hd_size"),
            duration,
        )
        add(
            "video_standard",
            "ویدیو بدون واترمارک",
            node.get("play"),
            "video/mp4",
            "mp4",
            node.get("size"),
            duration,
        )
        # A duplicate URL is one asset, not an extra advertised quality.
        if len(formats) == 2 and formats[0].endpoint == formats[1].endpoint:
            formats.pop()
            sources.pop("video_standard")
        if not formats:
            add(
                "video_watermark",
                "ویدیو با واترمارک",
                node.get("wmplay"),
                "video/mp4",
                "mp4",
                node.get("wm_size"),
                duration,
            )
    if not formats:
        raise DownloadError("فایل قابل دریافت برای این پست پیدا نشد.", code="tiktok_no_media")
    music = node.get("music_info") if isinstance(node.get("music_info"), dict) else {}
    add(
        "soundtrack",
        "موسیقی پست",
        node.get("music") or music.get("play"),
        "audio/mpeg",
        "mp3",
        item_duration=seconds(music.get("duration")),
    )
    media = Media(
        "tiktok",
        identity,
        str(node.get("title") or "TikTok"),
        username if username != "_" else str(author.get("nickname") or "TikTok"),
        duration,
        page,
        media_url(node.get("cover")),
        tuple(formats),
    )
    return Catalog(media, sources)


class WebState(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = None
        self.parts = []
        self.states = []

    def handle_starttag(self, tag, attrs):
        identity = dict(attrs).get("id")
        if tag == "script" and identity in {
            "__UNIVERSAL_DATA_FOR_REHYDRATION__",
            "SIGI_STATE",
            "sigi-persisted-data",
        }:
            self.active = identity
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            try:
                self.states.append(json.loads("".join(self.parts)))
            except ValueError:
                pass
            self.active, self.parts = None, []


def read_native(html: str, requested_url: str) -> Catalog:
    expected = content_id(requested_url)
    if not expected:
        raise DownloadError("مقصد لینک کوتاه تیک‌تاک مشخص نشد.", code="tiktok_short_link_unresolved")
    state = WebState()
    state.feed(html)
    for payload in state.states:
        if not isinstance(payload, dict):
            continue
        scope = payload.get("__DEFAULT_SCOPE__", {})
        detail = scope.get("webapp.video-detail", {}) if isinstance(scope, dict) else {}
        info = detail.get("itemInfo", {}) if isinstance(detail, dict) else {}
        node = info.get("itemStruct") if isinstance(info, dict) else None
        modules = payload.get("ItemModule", {})
        node = node or (modules.get(expected) if isinstance(modules, dict) else None)
        if isinstance(node, dict):
            return read_item(node, requested_url)
    raise DownloadError("صفحهٔ تیک‌تاک اطلاعات قابل دریافت نداشت.", code="tiktok_web_unavailable")


def read_item(node: dict, requested_url: str) -> Catalog:
    identity = checked_identity(node.get("id"), requested_url)
    if node.get("privateItem") or node.get("secret"):
        raise DownloadError("این پست تیک‌تاک عمومی نیست.", code="tiktok_private_media")
    video = node.get("video") if isinstance(node.get("video"), dict) else {}
    author = node.get("author")
    username = (
        str(author.get("uniqueId") or "_") if isinstance(author, dict) else str(author or "_")
    )
    duration = seconds(video.get("duration"))
    formats, sources = [], {}
    seen = set()
    bitrate_info = video.get("bitrateInfo") if isinstance(video.get("bitrateInfo"), list) else []
    candidates = [
        (item.get("PlayAddr", {}), item) for item in bitrate_info[:24] if isinstance(item, dict)
    ]
    candidates.append((video.get("playAddr"), video))
    for address, entry in candidates:
        url = first_url(address)
        codec = str(entry.get("CodecType") or video.get("codecType") or "h264").lower()
        if not url or url in seen or "bytevc2" in codec or "/media-video-hvc1/" in url:
            continue
        if codec in {"bytevc1", "hevc", "h265"}:
            codec = "hevc"
        elif codec in {"h264", "avc", "avc1"}:
            codec = "h264"
        else:
            continue
        seen.add(url)
        addr = address if isinstance(address, dict) else {}
        width = number(addr.get("Width") or entry.get("width"))
        height = number(addr.get("Height") or entry.get("height"))
        bitrate = number(entry.get("Bitrate"))
        size = number(addr.get("DataSize"))
        key = f"native_{width or 0}x{height or 0}_{codec}_{bitrate or 0}"
        if key in sources:
            continue
        label = f"{width}×{height}" if width and height else "ویدیو"
        label += f" · {codec.upper()}"
        if bitrate:
            label += f" · {bitrate // 1000} kbps"
        quality = Quality(
            key,
            label,
            codec,
            bitrate // 1000 if bitrate else None,
            "mp4",
            "video/mp4",
            "progressive",
            url,
            width=width,
            height=height,
            duration=duration,
        )
        formats.append(quality)
        sources[key] = Source(
            url,
            "progressive",
            size_bytes=size,
            duration=duration or None,
            require_audio=bool(node.get("music")),
        )
    if not formats:
        raise DownloadError("ویدیوی کامل قابل دریافت پیدا نشد.", code="tiktok_no_media")
    formats.sort(
        key=lambda q: (-(q.width or 0) * (q.height or 0), q.codec != "h264", -(q.bitrate or 0))
    )
    return Catalog(
        Media(
            "tiktok",
            identity,
            str(node.get("desc") or "TikTok"),
            username,
            duration,
            canonical_url(identity, username),
            first_url(video.get("cover")),
            tuple(formats),
        ),
        sources,
    )
