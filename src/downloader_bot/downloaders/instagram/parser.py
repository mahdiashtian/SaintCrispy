import json
import math
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.etree import ElementTree

from downloader_bot.schemas.media import DownloadError, Media, Quality, Source

from .urls import PAGE_HOSTS as HOSTS

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def validate_page_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in HOSTS
            and not parsed.port
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        valid = False
    if not valid:
        raise DownloadError("آدرس اینستاگرام معتبر نیست.")


def content_path(url: str) -> tuple[str, str]:
    validate_page_url(url)
    path = urlsplit(url).path
    if match := re.fullmatch(
        r"/(?!share/)(?:[\w.]+/)?(?:p|reels?|tv)/([A-Za-z0-9_-]{1,64})/?", path
    ):
        return "post", match[1]
    if match := re.fullmatch(r"/share/(?:reel/|p/)?([A-Za-z0-9_-]{1,64})/?", path):
        return "share", match[1]
    if match := re.fullmatch(r"/stories/([\w.]{1,30})/(\d{1,30})/?", path):
        return "story", match[1] + "/" + match[2]
    raise DownloadError("لینک یک پست، ریلز یا استوری مشخص اینستاگرام را بفرست.")


def media_id(code: str) -> str:
    # Private share codes can append an access token; only the shortcode encodes the ID.
    if len(code) > 28:
        code = code[:-28]
    value = 0
    for char in code:
        value = value * 64 + ALPHABET.index(char)
    return str(value)


def item_identity(item: dict, parent: str, *, child: bool) -> str:
    identifier = str(item.get("pk") or item.get("id") or "").removeprefix("POLARIS_")
    # Mobile API IDs can append the owner ID; Relay uses a POLARIS_ prefix.
    if re.fullmatch(r"\d+(?:_\d+)?", identifier):
        return str(int(identifier.split("_", 1)[0]))
    if identifier:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
            return identifier
        raise DownloadError("شناسه بخش اینستاگرام معتبر نیست.")
    if code := item.get("code") or item.get("shortcode"):
        return media_id(code)
    if child:
        raise DownloadError("شناسه پایدار یکی از بخش‌های آلبوم اینستاگرام موجود نیست.")
    return parent


def cdn_url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 16384:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname or ""
    if (
        parsed.scheme == "https"
        and not port
        and not parsed.username
        and not parsed.password
        and any(
            host.endswith("." + domain) or host == domain
            for domain in ("cdninstagram.com", "fbcdn.net")
        )
    ):
        return value
    return None


def find_media(data, code: str) -> dict | None:
    """Read the current product shape and older Relay/shortcode response shapes."""
    pending = [data]
    count = 0
    while pending and count < 20000:
        node = pending.pop()
        count += 1
        if isinstance(node, dict):
            identity = node.get("code") or node.get("shortcode")
            if identity == code and any(
                key in node
                for key in (
                    "video_versions",
                    "video_url",
                    "image_versions2",
                    "display_url",
                    "carousel_media",
                    "edge_sidecar_to_children",
                )
            ):
                return node
            # POLARIS wrapper and underlying product have the same code.
            pending.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            pending.extend(reversed(node))
        elif isinstance(node, str) and node.startswith(("{", "[")):
            try:
                pending.append(json.loads(node))
            except (ValueError, RecursionError):
                pass
    return None


class PageData(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._parts = []

    def handle_data(self, data):
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


def page_media(page: str, code: str) -> dict | None:
    parser = PageData()
    parser.feed(page)
    for script in parser.scripts:
        script = re.sub(r"^\s*window\._sharedData\s*=\s*", "", script).strip().rstrip(";")
        if not script.startswith(("{", "[")):
            continue
        try:
            node = find_media(json.loads(script), code)
        except (ValueError, RecursionError):
            continue
        if node:
            return node
    return None


def positive(value) -> int | None:
    try:
        number = int(value)
        return number if 0 < number < 2**40 else None
    except (ValueError, TypeError, OverflowError):
        return None


def duration(value) -> int:
    try:
        number = float(value)
        return math.ceil(number) if 0 < number < 86400 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


@dataclass(frozen=True)
class Format:
    quality: Quality
    source: Source


def dash_formats(xml: str, item_key: str, has_audio, prefix: str) -> tuple[list[Format], int]:
    if len(xml) > 512 * 1024 or re.search(r"<!DOCTYPE|<!ENTITY", xml, re.I):
        return [], 0
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return [], 0
    periods = root.findall("{*}Period")
    if root.get("type", "static") != "static" or len(periods) != 1:
        return [], 0
    match = re.fullmatch(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", root.get("mediaPresentationDuration", "")
    )
    seconds = (
        duration(sum(float(v or 0) * f for v, f in zip(match.groups(), (3600, 60, 1))))
        if match
        else 0
    )
    tracks = []
    for adaptation in periods[0].findall("{*}AdaptationSet"):
        for representation in adaptation.findall("{*}Representation"):
            attrs = {**adaptation.attrib, **representation.attrib}
            # These URLs represent full files with SegmentBase byte ranges. A template/list
            # describes separate segments; its BaseURL alone would produce an incomplete file.
            if any(
                node.find("{*}" + tag) is not None
                for node in (root, periods[0], adaptation, representation)
                for tag in ("ContentProtection", "SegmentTemplate", "SegmentList")
            ):
                continue
            url = cdn_url(representation.findtext("{*}BaseURL"))
            if url:
                tracks.append((attrs, url))
    audio = max(
        (
            t
            for t in tracks
            if t[0].get("mimeType") == "audio/mp4" and t[0].get("codecs", "").startswith("mp4a")
        ),
        key=lambda t: positive(t[0].get("bandwidth")) or 0,
        default=None,
    )
    if not audio and has_audio is not False:
        return [], seconds
    formats = []
    for attrs, url in tracks:
        codec = attrs.get("codecs", "")
        width, height = positive(attrs.get("width")), positive(attrs.get("height"))
        if attrs.get("mimeType") != "video/mp4" or not width or not height:
            continue
        if not codec.startswith(("avc1", "avc3", "vp09", "av01", "hvc1", "hev1")):
            continue
        encoding = attrs.get("FBEncodingTag") or f"{width}x{height}_{codec}"
        # Encoding tags stay stable when signed URLs and representation IDs change.
        encoding = re.sub(r"[^\w.-]", "_", encoding)[:100]
        bitrate = (positive(attrs.get("bandwidth")) or 0) + (
            (positive(audio[0].get("bandwidth")) or 0) if audio else 0
        )
        label = f"{prefix}{width}×{height} · {codec.split('.')[0].upper()} · {bitrate // 1000} kbps"
        # An explicitly silent full MP4 needs no remux: Telegram can fetch every
        # rendition directly, even though its URL came from a DASH manifest.
        protocol = "dash" if audio else "progressive"
        quality = Quality(
            f"{item_key}_dash_{encoding}_{width}x{height}",
            label,
            codec,
            bitrate // 1000 or None,
            "mp4",
            "video/mp4",
            protocol,
            url,
            width=width,
            height=height,
            duration=seconds,
        )
        formats.append(
            Format(
                quality,
                Source(
                    url,
                    protocol,
                    audio_url=audio[1] if audio else None,
                    require_audio=bool(audio),
                    duration=seconds or None,
                ),
            )
        )
    formats.sort(
        key=lambda f: ((f.quality.width or 0) * (f.quality.height or 0), f.quality.bitrate or 0),
        reverse=True,
    )
    return formats, seconds


def read_media(node: dict, page_url: str, content_id: str) -> tuple[Media, list[Format]]:
    user = node.get("user") or node.get("owner") or {}
    artist = str(user.get("username") or "Instagram")[:100]
    caption = node.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    if not isinstance(caption, str):
        edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
        caption = (edges[0].get("node") or {}).get("text") if edges else ""
    title = str(caption or f"Instagram @{artist}").replace("\n", " ")[:180]
    children = node.get("carousel_media")
    if not children:
        edges = (node.get("edge_sidecar_to_children") or {}).get("edges") or []
        children = [e["node"] for e in edges if isinstance(e.get("node"), dict)]
    items = children or [node]
    if len(items) > 20:
        raise DownloadError("تعداد بخش‌های این پست بیش از حد پشتیبانی‌شده است.")
    formats, seconds, thumbnail = [], 0, None
    for index, item in enumerate(items, 1):
        item_key = item_identity(item, content_id, child=bool(children))
        prefix = f"بخش {index} · " if children else ""
        images = (
            (item.get("image_versions2") or {}).get("candidates")
            or item.get("display_resources")
            or []
        )
        display = cdn_url(item.get("display_url") or item.get("display_uri"))
        thumbnail = (
            thumbnail
            or display
            or next((cdn_url(v.get("url")) for v in images if cdn_url(v.get("url"))), None)
        )
        videos = item.get("video_versions") or []
        if url := cdn_url(item.get("video_url")):
            dims = item.get("dimensions") or {}
            videos = [
                *videos,
                {
                    "url": url,
                    "width": dims.get("width"),
                    "height": dims.get("height"),
                    "type": "web",
                },
            ]
        item_duration = duration(item.get("video_duration"))
        dash, dash_duration = dash_formats(
            item.get("video_dash_manifest") or "", item_key, item.get("has_audio"), prefix
        )
        item_duration = item_duration or dash_duration
        seconds = max(seconds, item_duration)
        formats.extend(dash)
        for version in videos:
            if not (url := cdn_url(version.get("url"))):
                continue
            width, height = positive(version.get("width")), positive(version.get("height"))
            kind = re.sub(r"[^\w-]", "_", str(version.get("type", "web")))[:30]
            size = f"{width}×{height}" if width and height else f"گزینه {kind}"
            quality = Quality(
                f"{item_key}_mp4_{kind}",
                f"{prefix}MP4 · {size}",
                "video",
                None,
                "mp4",
                "video/mp4",
                "progressive",
                url,
                width=width,
                height=height,
                duration=item_duration,
            )
            formats.append(
                Format(
                    quality,
                    Source(
                        url,
                        "progressive",
                        require_audio=item.get("has_audio") is not False,
                        duration=item_duration or None,
                    ),
                )
            )
        is_video = bool(videos or dash or item.get("is_video") or item.get("media_type") == 2)
        if not is_video:
            for image in images or (
                [{"url": display, **(item.get("dimensions") or {})}] if display else []
            ):
                url = cdn_url(image.get("url") or image.get("src"))
                width = positive(image.get("width") or image.get("config_width"))
                height = positive(image.get("height") or image.get("config_height"))
                if not url or not width or not height:
                    continue
                quality = Quality(
                    f"{item_key}_jpg_{width}x{height}",
                    f"{prefix}عکس · {width}×{height}",
                    "jpeg",
                    None,
                    "jpg",
                    "image/jpeg",
                    "progressive",
                    url,
                    width=width,
                    height=height,
                    duration=0,
                )
                formats.append(Format(quality, Source(url, "progressive")))
    unique = {}
    for fmt in formats:
        unique.setdefault(fmt.quality.key, fmt)
    media = Media(
        "instagram",
        content_id,
        title,
        "@" + artist,
        seconds,
        page_url,
        thumbnail,
        tuple(f.quality for f in unique.values()),
    )
    return media, list(unique.values())


def mp4_info(data: bytes) -> tuple[int | None, int | None, bool | None]:
    """Read bounded moov metadata; never mistake the source's original size for a rendition."""
    start = data.find(b"moov")
    if start < 4:
        return None, None, None
    size = int.from_bytes(data[start - 4 : start], "big")
    if size < 16 or start - 4 + size > len(data):
        return None, None, None

    def boxes(blob):
        offset = 0
        while offset + 8 <= len(blob):
            length = int.from_bytes(blob[offset : offset + 4], "big")
            if length < 8 or offset + length > len(blob):
                return
            yield blob[offset + 4 : offset + 8], blob[offset + 8 : offset + length]
            offset += length

    width = height = None
    audio = False
    for kind, track in boxes(data[start + 4 : start - 4 + size]):
        if kind != b"trak":
            continue
        dimensions = None
        for tag, value in boxes(track):
            if tag == b"tkhd" and len(value) >= 84:
                dimensions = (
                    int.from_bytes(value[-8:-4], "big") >> 16,
                    int.from_bytes(value[-4:], "big") >> 16,
                )
            if tag == b"mdia":
                for child, payload in boxes(value):
                    if child == b"hdlr" and len(payload) >= 12:
                        audio |= payload[8:12] == b"soun"
        if dimensions and all(dimensions):
            width, height = dimensions
    return width, height, audio
