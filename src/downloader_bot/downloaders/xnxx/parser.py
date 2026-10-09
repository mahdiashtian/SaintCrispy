import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from math import isfinite
from urllib.parse import urljoin, urlsplit

from downloader_bot.models import DownloadError, Media, Quality

from .urls import PAGE_HOSTS


def media_url(value: str | None, page_url: str) -> str | None:
    if not value:
        return None
    try:
        value = unescape(value)
        if any(ord(character) < 32 for character in value):
            return None
        value = value.strip()
        if not value or any(ord(character) < 32 or character.isspace() for character in value):
            return None
        url = urljoin(page_url, value)
        parts = urlsplit(url)
        if (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and parts.username is None
            and parts.password is None
            and parts.port != 0
        ):
            return url
    except ValueError:
        pass
    return None


def duration_seconds(value: str | None) -> int:
    try:
        seconds = float(value)
        return int(seconds) if isfinite(seconds) and 0 <= seconds <= 2_147_483_647 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class HLSVariant:
    url: str
    bandwidth: int
    width: int | None
    height: int | None
    codec: str
    audio_url: str | None
    require_audio: bool = False

    def quality(self) -> Quality:
        identity = (
            f"{self.width}x{self.height}" if self.width and self.height else str(self.bandwidth)
        )
        label = (
            f"HLS {self.width}×{self.height}"
            if self.width and self.height
            else (f"HLS {self.bandwidth // 1000} kbps" if self.bandwidth else "HLS")
        )
        return Quality(
            f"hls_{identity}_{self.codec}",
            label,
            self.codec,
            self.bandwidth // 1000 or None,
            "mp4",
            "video/mp4",
            "hls",
            self.url,
            width=self.width,
            height=self.height,
        )


def hls_attributes(line: str) -> dict[str, str]:
    return {
        match[1]: match[2].strip('"')
        for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.partition(":")[2])
    }


def read_hls_variants(manifest: str, manifest_url: str) -> tuple[HLSVariant, ...]:
    lines = [line.strip() for line in manifest.lstrip("\ufeff").splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise DownloadError("فهرست پخش HLS معتبر نیست.")
    audio_groups: dict[str, dict[str, str]] = {}
    for line in lines:
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attributes = hls_attributes(line)
        if attributes.get("TYPE") == "AUDIO" and attributes.get("GROUP-ID"):
            group = attributes["GROUP-ID"]
            if group not in audio_groups or attributes.get("DEFAULT") == "YES":
                audio_groups[group] = attributes
    variants: dict[str, HLSVariant] = {}
    attributes = None
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            attributes = hls_attributes(line)
        elif not line.startswith("#") and attributes is not None:
            url = media_url(line, manifest_url)
            resolution = re.fullmatch(r"(\d+)x(\d+)", attributes.get("RESOLUTION", ""))
            codecs = attributes.get("CODECS", "").lower()
            # Exclude audio-only variants from the video menu.
            video_codec = next(
                (
                    name
                    for prefix, name in (
                        ("avc", "h264"),
                        ("hev", "h265"),
                        ("hvc", "h265"),
                        ("av01", "av1"),
                        ("vp09", "vp9"),
                    )
                    if prefix in codecs
                ),
                "video",
            )
            has_video = resolution or video_codec != "video" or not codecs
            rendition = audio_groups.get(attributes.get("AUDIO", ""), {})
            audio = media_url(rendition.get("URI"), manifest_url)
            group_valid = not attributes.get("AUDIO") or bool(rendition)
            if url and has_video and group_valid and (not rendition.get("URI") or audio):
                variant = HLSVariant(
                    url,
                    duration_seconds(
                        attributes.get("AVERAGE-BANDWIDTH") or attributes.get("BANDWIDTH")
                    ),
                    int(resolution[1]) or None if resolution else None,
                    int(resolution[2]) or None if resolution else None,
                    video_codec,
                    audio,
                    bool(rendition)
                    or any(name in codecs for name in ("mp4a", "opus", "ac-3", "ec-3")),
                )
                key = variant.quality().key
                if key not in variants or variant.bandwidth > variants[key].bandwidth:
                    variants[key] = variant
            attributes = None
    return tuple(
        sorted(
            variants.values(), key=lambda item: (item.height or 0, item.bandwidth), reverse=True
        )[:16]
    )


def hls_duration(manifest: str, manifest_url: str, expected_duration: int = 0) -> int:
    lines = [line.strip() for line in manifest.lstrip("\ufeff").splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        raise DownloadError("فهرست HLS کامل و نهایی نیست.")
    seconds = 0.0
    segments = 0
    pending_duration = None
    for line in lines:
        if line.startswith("#EXT-X-GAP"):
            raise DownloadError("یکی از قطعه‌های HLS موجود نیست.")
        if line.startswith(("#EXT-X-KEY:", "#EXT-X-MAP:")):
            attributes = hls_attributes(line)
            if attributes.get("URI") and not media_url(attributes["URI"], manifest_url):
                raise DownloadError("آدرس منبع HLS معتبر نیست.")
            if line.startswith("#EXT-X-KEY:") and (
                attributes.get("METHOD") not in {"NONE", "AES-128"}
                or attributes.get("KEYFORMAT", "identity") != "identity"
                or (attributes.get("METHOD") == "AES-128" and not attributes.get("URI"))
            ):
                raise DownloadError("روش پخش این HLS پشتیبانی نشده است.")
        if line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line.partition(":")[2].split(",", 1)[0])
                if not isfinite(pending_duration) or pending_duration <= 0:
                    raise ValueError
            except ValueError:
                raise DownloadError("مدت قطعه HLS معتبر نیست.") from None
        elif not line.startswith("#"):
            if pending_duration is None or not media_url(line, manifest_url):
                raise DownloadError("قطعه HLS معتبر نیست.")
            seconds += pending_duration
            segments += 1
            pending_duration = None
    if not segments or pending_duration is not None or "#EXT-X-STREAM-INF:" in manifest:
        raise DownloadError("فهرست قطعه‌های HLS معتبر نیست.")
    if expected_duration and seconds < expected_duration - max(3, expected_duration * 0.05):
        raise DownloadError("فهرست HLS فقط بخشی از ویدیو را دارد.")
    return duration_seconds(str(seconds))


def validate_page_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname in PAGE_HOSTS
            and parts.username is None
            and parts.password is None
            and parts.port in (None, 443)
            and not any(ord(character) < 32 or character.isspace() for character in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise DownloadError("لینک معتبر HTTPS از XNXX بفرست.")
    return url


class PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta":
            key = attributes.get("property") or attributes.get("name")
            if key and attributes.get("content"):
                self.meta[key.lower()] = attributes["content"]
        elif tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)


def player_string(page: str, method: str) -> str | None:
    matches = re.finditer(
        r"\b"
        + re.escape(method)
        + r"\s*\(\s*(?P<quote>['\"])(?P<value>(?:\\.|(?!(?P=quote))[^\\])*+)(?P=quote)\s*\)",
        page,
        re.IGNORECASE | re.DOTALL,
    )

    def decode_escape(match):
        if match[1] or match[2]:
            return chr(int(match[1] or match[2], 16))
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(
            match[3], "" if match[3] in "\r\n" else match[3]
        )

    value = None
    for match in matches:
        decoded = re.sub(
            r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(.))",
            decode_escape,
            match["value"],
            flags=re.DOTALL,
        )
        if decoded.strip():
            # Join JavaScript UTF-16 surrogate pairs, including titles with emoji.
            value = decoded.encode("utf-16", errors="surrogatepass").decode(
                "utf-16", errors="replace"
            )
    return value


def player_url(page: str, method: str, page_url: str) -> str | None:
    return media_url(player_string(page, method), page_url)


def video_id(page_url: str) -> str:
    validate_page_url(page_url)
    identity = re.fullmatch(
        r"/video[-.]?([a-z0-9]+)(?:/[^?#]*)?", urlsplit(page_url).path, re.IGNORECASE
    )
    if not identity:
        raise DownloadError("لینک صفحه یک ویدیو از XNXX را بفرست.")
    return identity[1].lower()


def extract_links(page: str, page_url: str, *, fallback_links: bool = False) -> dict:
    """Extract the title and High, Low and HLS URLs from the supplied HTML."""
    metadata = PageMetadata()
    metadata.feed(page)
    title = (
        metadata.meta.get("og:title")
        or player_string(page, "setVideoTitle")
        or "".join(metadata.title)
        or "video"
    )
    title = unescape(title)
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    title = " ".join(title.split())[:400] or "video"
    duration = re.search(r"setVideoDuration\s*\(\s*['\"]?(\d+(?:\.\d+)?)", page, re.IGNORECASE)
    info = {
        "title": title,
        "high": player_url(page, "setVideoUrlHigh", page_url),
        "low": player_url(page, "setVideoUrlLow", page_url),
        "hls": player_url(page, "setVideoHLS", page_url),
        "page_url": page_url,
        "thumbnail": media_url(metadata.meta.get("og:image"), page_url)
        or player_url(page, "setThumbUrl", page_url)
        or player_url(page, "setThumbUrl169", page_url),
        "duration": duration_seconds(metadata.meta.get("og:duration"))
        or duration_seconds(metadata.meta.get("video:duration"))
        or duration_seconds(duration[1] if duration else None),
    }
    if fallback_links and not any(info[key] for key in ("high", "low", "hls")):
        # The supplied XVideos script also searches literal URLs when player calls are absent.
        for match in re.finditer(r"https?://[^\s<>\"']+", page, re.IGNORECASE):
            url = media_url(match[0], page_url)
            if url:
                extension = urlsplit(url).path.lower()
                if extension.endswith(".mp4") and not info["high"]:
                    info["high"] = url
                elif extension.endswith(".m3u8") and not info["hls"]:
                    info["hls"] = url
    return info


def read_video(
    page: str,
    page_url: str,
    *,
    fallback_links: bool = False,
) -> Media:
    identity = video_id(page_url)
    info = extract_links(page, page_url, fallback_links=fallback_links)
    qualities = tuple(
        Quality(
            key,
            label,
            "video",
            None,
            "mp4",
            "video/mp4",
            "hls" if urlsplit(info[key]).path.lower().endswith(".m3u8") else protocol,
            info[key],
        )
        for key, label, protocol in (
            ("high", "MP4 کیفیت بالا", "progressive"),
            ("hls", "HLS (کیفیت خودکار)", "hls"),
            ("low", "MP4 کیفیت پایین", "progressive"),
        )
        if info[key]
    )
    if not qualities:
        raise DownloadError("هیچ لینک قابل دانلودی در صفحه ویدیو پیدا نشد.")
    return Media(
        "xnxx",
        identity,
        info["title"],
        "",
        info["duration"],
        page_url,
        info["thumbnail"],
        qualities,
    )
