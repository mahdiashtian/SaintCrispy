import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from math import isfinite
from urllib.parse import unquote, urljoin, urlsplit

from downloader_bot.schemas.media import DownloadError, Media, Quality

from .urls import validate_page_url as validate_page_url
from .urls import video_id as video_id


def mp4_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read a complete video track header from a bounded, possibly partial moov.

    Missing metadata remains unknown; URL names and High/Low never imply pixels.
    Non-identity display matrices need a full media probe and are left unknown.
    """

    def boxes(start: int, boundary: int):
        offset = start
        while offset + 8 <= min(len(data), boundary):
            size = int.from_bytes(data[offset : offset + 4], "big")
            kind = data[offset + 4 : offset + 8]
            header = 8
            if size == 1:
                if offset + 16 > min(len(data), boundary):
                    return
                size = int.from_bytes(data[offset + 8 : offset + 16], "big")
                header = 16
            elif size == 0:
                size = boundary - offset
            end = offset + size
            if size < header or end > boundary:
                return
            yield kind, offset + header, end
            offset = end

    # The top-level boundary can exceed the sample when moov is incomplete.
    for kind, start, end in boxes(0, 2**64):
        if kind != b"moov":
            continue
        for kind, track_start, track_end in boxes(start, end):
            if kind != b"trak":
                continue
            dimensions = None
            video = False
            for kind, child_start, child_end in boxes(track_start, track_end):
                if kind == b"tkhd" and child_end <= len(data):
                    value = data[child_start:child_end]
                    if not value or value[0] not in {0, 1}:
                        continue
                    length = 96 if value[0] == 1 else 84
                    if len(value) != length or not value[3] & 1:
                        continue
                    matrix = tuple(
                        int.from_bytes(value[i : i + 4], "big")
                        for i in range(length - 44, length - 8, 4)
                    )
                    if matrix != (65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824):
                        continue
                    width = int.from_bytes(value[-8:-4], "big")
                    height = int.from_bytes(value[-4:], "big")
                    if (
                        not width & 65535
                        and not height & 65535
                        and 0 < width >> 16 <= 16384
                        and 0 < height >> 16 <= 16384
                    ):
                        dimensions = width >> 16, height >> 16
                elif kind == b"mdia":
                    for kind, handler_start, handler_end in boxes(child_start, child_end):
                        if kind == b"hdlr" and handler_start + 12 <= min(handler_end, len(data)):
                            video = data[handler_start + 8 : handler_start + 12] == b"vide"
                            break
            if video and dimensions:
                return dimensions
    return None


def media_url(value: str | None, page_url: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        value = unescape(value)
        if "\\" in value or any(ord(character) < 32 for character in value):
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
    variants: dict[tuple[str, str | None], HLSVariant] = {}
    attributes = None
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            attributes = hls_attributes(line)
        elif not line.startswith("#") and attributes is not None:
            url = media_url(line, manifest_url)
            resolution = re.fullmatch(
                r"([0-9]{1,5})x([0-9]{1,5})", attributes.get("RESOLUTION", "")
            )
            resolution_valid = not attributes.get("RESOLUTION") or bool(
                resolution and int(resolution[1]) > 0 and int(resolution[2]) > 0
            )
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
            if (
                url
                and has_video
                and resolution_valid
                and group_valid
                and (not rendition.get("URI") or audio)
            ):
                variant = HLSVariant(
                    url,
                    duration_seconds(
                        attributes.get("AVERAGE-BANDWIDTH") or attributes.get("BANDWIDTH")
                    ),
                    int(resolution[1]) or None if resolution else None,
                    int(resolution[2]) or None if resolution else None,
                    video_codec,
                    audio,
                    bool(
                        rendition
                        or any(name in codecs for name in ("mp4a", "ac-3", "ec-3", "opus"))
                    ),
                )
                variants[(variant.url, variant.audio_url)] = variant
            attributes = None
    return tuple(
        sorted(
            variants.values(), key=lambda item: (item.height or 0, item.bandwidth), reverse=True
        )[:32]
    )


@dataclass(frozen=True)
class HLSSample:
    url: str = field(repr=False)
    kind: str
    offset: int = 0
    length: int = 1024


@dataclass(frozen=True)
class HLSPlaylist:
    duration: float
    samples: tuple[HLSSample, ...]


def _byte_range(value: str, url: str, previous: tuple[str, int] | None) -> tuple[int, int]:
    match = re.fullmatch(r"([0-9]{1,20})(?:@([0-9]{1,20}))?", value)
    if not match or int(match[1]) <= 0:
        raise DownloadError("محدوده قطعه HLS معتبر نیست.")
    if match[2] is None:
        if previous is None or previous[0] != url:
            raise DownloadError("شروع محدوده قطعه HLS مشخص نیست.")
        offset = previous[1]
    else:
        offset = int(match[2])
    return offset, int(match[1])


def read_hls_playlist(manifest: str, manifest_url: str, expected_duration: int = 0) -> HLSPlaylist:
    lines = [line.strip() for line in manifest.lstrip("\ufeff").splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        raise DownloadError("فهرست HLS کامل و نهایی نیست.")
    if any(line.startswith(("#EXT-X-STREAM-INF:", "#EXT-X-I-FRAMES-ONLY")) for line in lines):
        raise DownloadError("فهرست قطعه‌های HLS معتبر نیست.")
    seconds = 0.0
    pending_duration = None
    pending_range = None
    previous_range = previous_map = None
    init = key = None
    first = last = None
    first_resources = last_resources = ()
    for line in lines:
        if line.startswith("#EXT-X-GAP"):
            raise DownloadError("یکی از قطعه‌های HLS موجود نیست.")
        if line.startswith("#EXT-X-KEY:"):
            attrs = hls_attributes(line)
            if attrs.get("METHOD") == "NONE":
                key = None
                continue
            url = media_url(attrs.get("URI"), manifest_url)
            if (
                attrs.get("METHOD") != "AES-128"
                or attrs.get("KEYFORMAT", "identity") != "identity"
                or not url
                or (attrs.get("IV") and not re.fullmatch(r"0x[0-9a-fA-F]{1,32}", attrs["IV"]))
            ):
                raise DownloadError("روش پخش این HLS پشتیبانی نشده است.")
            key = HLSSample(url, "key", length=17)
        elif line.startswith("#EXT-X-MAP:"):
            attrs = hls_attributes(line)
            url = media_url(attrs.get("URI"), manifest_url)
            if not url:
                raise DownloadError("آدرس منبع HLS معتبر نیست.")
            offset, length = (
                _byte_range(attrs["BYTERANGE"], url, previous_map)
                if attrs.get("BYTERANGE")
                else (0, 1024)
            )
            previous_map = (url, offset + length) if attrs.get("BYTERANGE") else None
            init = HLSSample(url, "encrypted" if key else "mp4", offset, min(length, 1024))
        elif line.startswith("#EXT-X-BYTERANGE:"):
            if pending_range is not None:
                raise DownloadError("محدوده قطعه HLS تکراری است.")
            pending_range = line.partition(":")[2]
        elif line.startswith("#EXTINF:"):
            if pending_duration is not None:
                raise DownloadError("آدرس یکی از قطعه‌های HLS موجود نیست.")
            try:
                pending_duration = float(line.partition(":")[2].split(",", 1)[0])
                if not isfinite(pending_duration) or pending_duration <= 0:
                    raise ValueError
            except ValueError:
                raise DownloadError("مدت قطعه HLS معتبر نیست.") from None
        elif not line.startswith("#"):
            url = media_url(line, manifest_url)
            if pending_duration is None or not url:
                raise DownloadError("قطعه HLS معتبر نیست.")
            seconds += pending_duration
            if not isfinite(seconds) or seconds > 2_147_483_647:
                raise DownloadError("مدت HLS معتبر نیست.")
            offset, length = (
                _byte_range(pending_range, url, previous_range)
                if pending_range is not None
                else (0, 1024)
            )
            previous_range = (url, offset + length) if pending_range is not None else None
            last = HLSSample(url, "encrypted" if key else "media", offset, min(length, 1024))
            last_resources = tuple(item for item in (init, key) if item)
            if first is None:
                first, first_resources = last, last_resources
            pending_duration = pending_range = None
    if first is None or pending_duration is not None or pending_range is not None:
        raise DownloadError("فهرست قطعه‌های HLS معتبر نیست.")
    if expected_duration and seconds < expected_duration - max(3, expected_duration * 0.05):
        raise DownloadError("فهرست HLS فقط بخشی از ویدیو را دارد.")
    samples = tuple(dict.fromkeys((*first_resources, first, *last_resources, last)))
    return HLSPlaylist(seconds, samples)


def hls_duration(manifest: str, manifest_url: str, expected_duration: int = 0) -> int:
    return int(read_hls_playlist(manifest, manifest_url, expected_duration).duration)


class PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title: list[str] = []
        self.in_title = False
        self.in_duration = False
        self.in_jsonld = False
        self.duration_text: list[str] = []
        self.jsonld: list[str] = []
        self.sources: list[str] = []
        self._json_parts: list[str] = []
        self.unavailable = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta":
            key = attributes.get("property") or attributes.get("name")
            if key and attributes.get("content"):
                self.meta[key.lower()] = attributes["content"]
        elif tag == "title":
            self.in_title = True
        elif tag == "h1" and "inlineError" in (attributes.get("class") or "").split():
            self.unavailable = True
        elif tag == "span" and "duration" in (attributes.get("class") or "").split():
            self.in_duration = True
        elif tag == "script" and (attributes.get("type") or "").lower() == "application/ld+json":
            self.in_jsonld = True
            self._json_parts = []
        elif tag in {"source", "video"} and attributes.get("src"):
            self.sources.append(attributes["src"])

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "span":
            self.in_duration = False
        elif tag == "script" and self.in_jsonld:
            self.jsonld.append("".join(self._json_parts))
            self.in_jsonld = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if self.in_duration:
            self.duration_text.append(data)
        if self.in_jsonld:
            self._json_parts.append(data)


def decode_player_string(value: str) -> str:
    def escape(match):
        if match[1] or match[2]:
            return chr(int(match[1] or match[2], 16))
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(
            match[3], "" if match[3] in "\r\n" else match[3]
        )

    decoded = re.sub(
        r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(.))", escape, value, flags=re.DOTALL
    )
    return decoded.encode("utf-16", errors="surrogatepass").decode("utf-16", errors="replace")


def player_strings(page: str, method: str):
    pattern = (
        r"\b"
        + re.escape(method)
        + r"\s*\(\s*(?P<quote>['\"])(?P<value>(?:\\.|(?!(?P=quote))[^\\])*+)(?P=quote)\s*\)"
    )
    for match in re.finditer(pattern, page, re.IGNORECASE | re.DOTALL):
        value = decode_player_string(match["value"])
        if value.strip():
            yield value


def player_string(page: str, method: str) -> str | None:
    return next(reversed(list(player_strings(page, method))), None)


def player_url(page: str, method: str, page_url: str) -> str | None:
    urls = [url for value in player_strings(page, method) if (url := media_url(value, page_url))]
    return urls[-1] if urls else None


def video_metadata(metadata: PageMetadata) -> dict:
    for text in metadata.jsonld:
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            continue
        pending = [data]
        for _ in range(64):
            if not pending:
                break
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(item[:16])
            elif isinstance(item, dict):
                kind = item.get("@type")
                if kind == "VideoObject" or isinstance(kind, list) and "VideoObject" in kind:
                    return item
                if isinstance(item.get("@graph"), (dict, list)):
                    pending.append(item["@graph"])
    return {}


def parse_duration(value: str | None) -> int:
    if not isinstance(value, str):
        return 0
    if match := re.fullmatch(r"PT(?:(\d{1,7})H)?(?:(\d{1,7})M)?(?:(\d{1,10}(?:\.\d+)?)S)?", value):
        return duration_seconds(
            str(float(match[1] or 0) * 3600 + float(match[2] or 0) * 60 + float(match[3] or 0))
        )
    if match := re.search(r"\b(\d{1,7}):(\d{2})(?::(\d{2}))?\b", value):
        numbers = [int(number) for number in match.groups() if number is not None]
        if any(number >= 60 for number in numbers[1:]):
            return 0
        seconds = 0
        for number in numbers:
            seconds = seconds * 60 + number
        return duration_seconds(str(seconds))
    return duration_seconds(value)


def extract_links(page: str, page_url: str, *, fallback_links: bool = True) -> dict:
    """Extract the title and High, Low and HLS URLs from the supplied HTML."""
    metadata = PageMetadata()
    metadata.feed(page)
    if metadata.unavailable:
        raise DownloadError("این ویدیو در XVideos موجود نیست یا با دسترسی فعلی قابل مشاهده نیست.")
    structured = video_metadata(metadata)
    title = (
        metadata.meta.get("og:title")
        or player_string(page, "setVideoTitle")
        or structured.get("name")
        or "".join(metadata.title)
        or "video"
    )
    title = unescape(str(title))
    title = re.sub(r"\s+-\s+XVIDEOS(?:\.COM)?\s*$", "", title, flags=re.IGNORECASE)
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
        or player_url(page, "setThumbUrl169", page_url)
        or player_url(page, "setThumbUrl", page_url)
        or media_url(
            (structured.get("thumbnailUrl") or [None])[0]
            if isinstance(structured.get("thumbnailUrl"), list)
            else structured.get("thumbnailUrl"),
            page_url,
        ),
        "duration": duration_seconds(metadata.meta.get("og:duration"))
        or duration_seconds(metadata.meta.get("video:duration"))
        or duration_seconds(duration[1] if duration else None)
        or parse_duration(structured.get("duration"))
        or parse_duration(" ".join(metadata.duration_text)),
    }
    if fallback_links and not any(info[key] for key in ("high", "low", "hls")):
        candidates = [
            *metadata.sources,
            structured.get("contentUrl"),
            metadata.meta.get("og:video"),
            metadata.meta.get("og:video:url"),
        ]
        for value in candidates:
            url = media_url(value, page_url) if isinstance(value, str) else None
            if url:
                extension = urlsplit(url).path.lower()
                if extension.endswith(".mp4") and not info["high"]:
                    info["high"] = url
                elif extension.endswith(".m3u8") and not info["hls"]:
                    info["hls"] = url
        if not info["high"]:
            # Legacy Flash parameters can also advertise an actual MP4 source.
            legacy = re.search(r"\bflv_url=([^&\s\"'<>]+)", unescape(page), re.IGNORECASE)
            url = media_url(unquote(legacy[1]), page_url) if legacy else None
            if url and urlsplit(url).path.lower().endswith(".mp4"):
                info["high"] = url
        if not info["hls"]:
            for match in re.finditer(r"https?:(?:\\?/){2}[^\s<>\"']+", page, re.IGNORECASE):
                url = media_url(decode_player_string(match[0]), page_url)
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
    fallback_links: bool = True,
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
        "xvideos",
        identity,
        info["title"],
        "",
        info["duration"],
        page_url,
        info["thumbnail"],
        qualities,
    )
