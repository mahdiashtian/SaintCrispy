import hashlib
import json
import re
import struct
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from math import isfinite
from urllib.parse import urljoin, urlsplit

from downloader_bot.schemas.media import DownloadError, Media, Quality

from .urls import PINTEREST_HOSTS


@dataclass(frozen=True)
class MediaProbe:
    valid: bool
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    codec: str | None = None
    size_bytes: int | None = None


def _boxes(data: bytes):
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                break
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header:
            break
        yield kind, data[offset + header : min(offset + size, len(data))]
        offset += size


def mp4_metadata(body: bytes, *, tail: bool = False) -> MediaProbe:
    """Read bounded MP4 box metadata; Pinterest's format dimensions may describe the source."""
    if tail:
        index = body.find(b"moov")
        if index < 4:
            return MediaProbe(False)
        size = struct.unpack_from(">I", body, index - 4)[0]
        if size < 8 or size > len(body) - index + 4:
            return MediaProbe(False)
        body = body[index - 4 : index - 4 + size]
    elif len(body) < 12 or body[4:8] != b"ftyp":
        return MediaProbe(False)
    width = height = duration = codec = None
    for kind, moov in _boxes(body):
        if kind != b"moov":
            continue
        for kind, payload in _boxes(moov):
            if kind == b"mvhd" and payload:
                version = payload[0]
                if version == 0 and len(payload) >= 20:
                    scale, ticks = struct.unpack_from(">II", payload, 12)
                elif version == 1 and len(payload) >= 32:
                    scale = struct.unpack_from(">I", payload, 20)[0]
                    ticks = struct.unpack_from(">Q", payload, 24)[0]
                else:
                    continue
                duration = duration_seconds(str(ticks / scale)) if scale else None
            elif kind == b"trak":
                track = dict(_boxes(payload))
                mdia = dict(_boxes(track.get(b"mdia", b"")))
                handler = mdia.get(b"hdlr", b"")
                tkhd = track.get(b"tkhd", b"")
                if len(handler) < 12 or handler[8:12] != b"vide" or not tkhd:
                    continue
                position = 76 if tkhd[0] == 0 else 88 if tkhd[0] == 1 else None
                if position is not None and len(tkhd) >= position + 8:
                    w, h = struct.unpack_from(">II", tkhd, position)
                    width, height = w >> 16 or None, h >> 16 or None
                minf = dict(_boxes(mdia.get(b"minf", b"")))
                stbl = dict(_boxes(minf.get(b"stbl", b"")))
                stsd = stbl.get(b"stsd", b"")
                for name, _ in _boxes(stsd[8:]):
                    codec = {
                        b"avc1": "h264",
                        b"avc3": "h264",
                        b"hvc1": "h265",
                        b"hev1": "h265",
                        b"av01": "av1",
                        b"vp09": "vp9",
                    }.get(name)
                    if codec:
                        break
    return MediaProbe(True, width, height, duration, codec)


def validate_page_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname in PINTEREST_HOSTS | {"pin.it"}
            and not parts.username
            and not parts.password
            and parts.port in (None, 443)
            and not any(character.isspace() or ord(character) < 32 for character in url)
            and "\\" not in url
        )
    except ValueError:
        valid = False
    if not valid:
        raise DownloadError("لینک معتبر HTTPS از Pinterest بفرست.")
    return url


def pin_id(url: str) -> str:
    validate_page_url(url)
    match = re.fullmatch(r"/pin/(?:[\w-]+--)?(\d{1,30})(?:/(?:sent/)?)?", urlsplit(url).path)
    if not match:
        raise DownloadError(
            "لینک یک پین Pinterest را بفرست؛ دانلود برد و پروفایل پشتیبانی نشده است."
        )
    return match[1]


def cdn_url(value) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (
            parts.scheme == "https"
            and host.endswith(".pinimg.com")
            and not parts.username
            and not parts.password
            and parts.port in (None, 443)
            and not any(character.isspace() or ord(character) < 32 for character in value)
        ):
            return value
    except ValueError:
        pass
    return None


def read_api_pin(body: bytes, identity: str) -> dict:
    try:
        response = json.loads(body).get("resource_response", {})
        data = response.get("data")
    except (ValueError, AttributeError, RecursionError):
        data = None
    if not isinstance(data, dict) or str(data.get("id")) != identity:
        raise DownloadError("اطلاعات این پین در دسترس نیست؛ ممکن است خصوصی یا حذف شده باشد.")
    return data


class PinScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = False
        self.chunks: list[str] = []
        self.documents: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            self.active = not attributes.get("src")
            self.chunks = []

    def handle_data(self, data):
        if self.active:
            self.chunks.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.documents.append("".join(self.chunks))
            self.active = False


def read_page_pin(page: str, identity: str) -> dict:
    """Parse JSON literals in current Relay and legacy pages without executing JavaScript."""
    scripts = PinScripts()
    scripts.feed(page)
    fallback = None
    for document in scripts.documents:
        documents = []
        try:
            documents.append(json.loads(document))
        except (ValueError, RecursionError):
            decoder = json.JSONDecoder()
            for match in re.finditer(
                r"window\.__PWS_RELAY_REGISTER_COMPLETED_REQUEST__\s*\(", document
            ):
                try:
                    args = document[match.end() :].lstrip()
                    name, end = decoder.raw_decode(args)
                    args = args[end:].lstrip()
                    if isinstance(name, str) and args.startswith(","):
                        payload, _ = decoder.raw_decode(args[1:].lstrip())
                        documents.append(payload)
                except (ValueError, RecursionError):
                    continue
        stack = documents
        visited = 0
        while stack and visited < 50000:
            item = stack.pop()
            visited += 1
            if isinstance(item, dict):
                if str(item.get("entityId")) == identity and any(
                    key in item for key in ("images_orig", "videos", "storyPinData")
                ):
                    normalized = _normalize_relay(item)
                    # Prefer the closeup response, which includes videoUrls, over grid previews.
                    if (normalized.get("videos") or {}).get("video_urls"):
                        return normalized
                    if fallback is None or len(normalized.get("images") or {}) > len(
                        fallback.get("images") or {}
                    ):
                        fallback = normalized
                if str(item.get("id")) == identity and any(
                    key in item for key in ("images", "videos", "story_pin_data", "carousel_data")
                ):
                    return item
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    if fallback is not None:
        return fallback
    raise DownloadError("دادهٔ رسانهٔ این پین از Pinterest دریافت نشد.")


def _normalize_relay(value):
    if isinstance(value, list):
        return [_normalize_relay(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    images = {}
    for key, item in value.items():
        if key == "__typename":
            continue
        if key.startswith("images_") and isinstance(item, dict):
            images[key[7:]] = _normalize_relay(item)
        elif key == "videoList" and isinstance(item, dict):
            names = {
                "v720P": "V_720P",
                "vHLSV4": "V_HLSV4",
                "vHLSV3MOBILE": "V_HLSV3_MOBILE",
                "vHLSV3WEB": "V_HLSV3_WEB",
            }
            result["video_list"] = {
                names.get(name, name): _normalize_relay(fmt)
                for name, fmt in item.items()
                if isinstance(fmt, dict)
            }
        else:
            normalized = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", key).lower()
            result[normalized] = _normalize_relay(item)
    if images:
        result["images"] = images
    if value.get("entityId"):
        result["id"] = str(value["entityId"])
    return result


def _asset_key(value, fallback: str) -> str:
    return hashlib.sha256(str(value or fallback).encode()).hexdigest()[:16]


def _asset_identity(value: dict, native=None) -> str:
    if native:
        return str(native)
    # Never key a carousel block by its position. Immutable CDN signatures are
    # shared by renditions; delivery hosts and signed queries can change.
    urls = []
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif url := cdn_url(item):
            path = urlsplit(url).path
            signature = re.search(r"[0-9a-f]{32}", path, re.IGNORECASE)
            urls.append(signature[0].lower() if signature else path)
    if not urls:
        raise DownloadError("شناسه پایدار رسانه این پین پیدا نشد.")
    return "cdn:" + hashlib.sha256(json.dumps(sorted(set(urls))).encode()).hexdigest()


def _dimensions(value: dict) -> tuple[int | None, int | None]:
    return tuple(duration_seconds(str(value.get(key))) or None for key in ("width", "height"))


def _image_qualities(images: dict, asset: str, prefix: str) -> list[Quality]:
    formats = []
    seen = set()
    for size, value in sorted(
        images.items(), key=lambda item: item[0] not in {"orig", "originals"}
    ):
        if not isinstance(value, dict) or not (url := cdn_url(value.get("url"))):
            continue
        # Cropped thumbnails are previews, not alternate qualities of the whole image.
        if re.fullmatch(r"\d+x\d+", size) or url in seen:
            continue
        extension = urlsplit(url).path.rsplit(".", 1)[-1].lower()
        mime = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "gif": "image/gif",
        }.get(extension)
        if not mime:
            continue
        width, height = _dimensions(value)
        original = size in {"orig", "originals"}
        dimensions = f"{width}×{height}" if width and height else size
        label = f"{prefix}{'اصلی' if original else 'تصویر'} {dimensions} ({extension.upper()})"
        key = f"i_{asset}_{size}_{extension}_{width or 0}x{height or 0}"
        formats.append(
            Quality(
                key,
                label,
                extension,
                None,
                extension,
                mime,
                "progressive",
                url,
                original=original,
                width=width,
                height=height,
            )
        )
        seen.add(url)
    return formats


def _video_qualities(video: dict, asset: str, prefix: str) -> tuple[list[Quality], int]:
    formats = []
    seconds = 0
    seen = set()
    entries = video.get("video_list") or {}
    if not isinstance(entries, dict):
        return formats, seconds
    entries = dict(entries)
    for url in video.get("video_urls") or []:
        if cdn_url(url) and urlsplit(url).path.lower().endswith(".mp4"):
            # These are published URLs, never guessed path substitutions.
            name = "CDN_" + _asset_key(urlsplit(url).path, "")
            entries[name] = {"url": url, "duration": video.get("duration")}
    for name, value in sorted(
        entries.items(), key=lambda item: (item[0].startswith("CDN_"), item[0])
    ):
        if not isinstance(value, dict) or not (url := cdn_url(value.get("url"))) or url in seen:
            continue
        width, height = _dimensions(value)
        seconds = max(seconds, duration_seconds(str(value.get("duration"))) // 1000)
        extension = urlsplit(url).path.rsplit(".", 1)[-1].lower()
        if extension not in {"mp4", "m3u8"}:
            continue
        protocol = "hls" if extension == "m3u8" else "progressive"
        dimensions = f"{width}×{height}" if width and height else "ابعاد نامشخص"
        # Source names distinguish real encodes; they are not resolution labels.
        safe_name = re.sub(r"[^a-z0-9_]", "_", name.lower())[:40]
        key = f"v_{asset}_{safe_name}_{width or 0}x{height or 0}"
        formats.append(
            Quality(
                key,
                f"{prefix}{'HLS' if protocol == 'hls' else 'MP4'} {dimensions} · {name}",
                "h264",
                None,
                "mp4",
                "video/mp4",
                protocol,
                url,
                width=width,
                height=height,
                duration=duration_seconds(str(value.get("duration"))) // 1000,
            )
        )
        seen.add(url)
    return formats, seconds


def read_pin(data: dict, identity: str) -> Media:
    if str(data.get("id")) != identity:
        raise DownloadError("شناسهٔ پین با لینک مطابقت ندارد.")
    assets: list[tuple[str, dict, str]] = []
    story = data.get("story_pin_data") if isinstance(data.get("story_pin_data"), dict) else {}
    carousel = data.get("carousel_data") if isinstance(data.get("carousel_data"), dict) else {}
    for page in story.get("pages", []):
        for block in page.get("blocks", []):
            video = block.get("video")
            image = block.get("image") or {}
            if isinstance(video, dict):
                assets.append(
                    (
                        "video",
                        video,
                        _asset_identity(video, video.get("id") or block.get("id")),
                    )
                )
            elif block.get("type") == "story_pin_image_block" and image.get("images"):
                assets.append(
                    (
                        "image",
                        image["images"],
                        _asset_identity(image["images"], block.get("id")),
                    )
                )
    if not assets:
        for slot in carousel.get("carousel_slots", []):
            if slot.get("images"):
                assets.append(
                    ("image", slot["images"], _asset_identity(slot["images"], slot.get("id")))
                )
    if not assets:
        if isinstance(data.get("videos"), dict) and data["videos"].get("video_list"):
            assets.append(
                ("video", data["videos"], _asset_identity(data["videos"], data["videos"].get("id")))
            )
        elif (
            data.get("is_video")
            or story.get("pages")
            or (isinstance(data.get("embed"), dict) and data["embed"].get("src"))
        ):
            raise DownloadError(
                "ویدیوی این پین در دسترس نیست؛ تصویر پیش‌نمایش جای ویدیو ارسال نمی‌شود."
            )
        else:
            assets.append(
                (
                    "image",
                    data.get("images") or {},
                    _asset_identity(data.get("images") or {}, data.get("image_signature")),
                )
            )
    if len(assets) > 16:
        raise DownloadError("این پین بیش از ۱۶ رسانه دارد و فعلاً پشتیبانی نشده است.")
    formats = []
    seconds = 0
    for index, (kind, value, identifier) in enumerate(assets, 1):
        asset = _asset_key(identifier, str(index))
        prefix = f"رسانه {index} · " if len(assets) > 1 else ""
        if kind == "video":
            qualities, duration = _video_qualities(value, asset, prefix)
            formats.extend(qualities)
            seconds = max(seconds, duration)
        else:
            formats.extend(_image_qualities(value, asset, prefix))
    if not formats:
        raise DownloadError("رسانهٔ قابل دانلودی روی CDN خود Pinterest پیدا نشد.")
    images = data.get("images") or {}
    thumbnail = next(
        (
            cdn_url(images[key].get("url"))
            for key in ("474x", "736x", "orig")
            if isinstance(images.get(key), dict) and cdn_url(images[key].get("url"))
        ),
        None,
    )
    author = data.get("native_creator") or data.get("pinner") or {}
    return Media(
        "pinterest",
        identity,
        str(
            data.get("title")
            or data.get("grid_title")
            or data.get("seo_title")
            or f"Pinterest {identity}"
        )[:300],
        str(author.get("full_name") or author.get("username") or ""),
        seconds,
        f"https://www.pinterest.com/pin/{identity}/",
        thumbnail,
        tuple(formats),
    )


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
