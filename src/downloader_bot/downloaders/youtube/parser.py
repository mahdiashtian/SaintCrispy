import json
import math
import re
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from itertools import islice
from urllib.parse import parse_qs, urljoin, urlsplit

from schemas.media import DownloadError, Media, Quality, Source

from .urls import PAGE_HOSTS as HOSTS

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
# Stay below YouTube's documented 10 MB HTTP-request throttling threshold.
HTTP_CHUNK_SIZE = 8 * 1024 * 1024


def video_id(url: str) -> str:
    try:
        for _ in range(4):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in HOSTS
                or parsed.port is not None
                or parsed.username
                or parsed.password
                or "\\" in url
                or any(character.isspace() or ord(character) < 32 for character in url)
            ):
                raise ValueError
            query = parse_qs(parsed.query, max_num_fields=128)
            if parsed.path in {"/attribution_link", "/redirect", "/oembed"}:
                key = {"/attribution_link": "u", "/redirect": "q", "/oembed": "url"}[parsed.path]
                targets = query.get(key, [])
                if len(targets) != 1:
                    raise ValueError
                url = urljoin("https://www.youtube.com/", targets[0])
                continue
            if parsed.path == "/" and parsed.fragment.startswith("/watch?"):
                url = urljoin("https://www.youtube.com/", parsed.fragment)
                continue
            break
        else:
            raise ValueError
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname in {"youtu.be", "www.youtu.be"} and len(parts) == 1:
            identity = parts[0]
        elif parsed.path.rstrip("/") in {
            "",
            "/watch",
            "/watch_popup",
            "/watch.php",
            "/movie",
            "/movie_popup",
        }:
            values = query.get("v", [])
            identity = values[0] if len(values) == 1 else ""
        elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live", "v", "e"}:
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


@dataclass(frozen=True)
class WatchMetadata:
    title: str
    author: str
    duration: int
    playable: bool
    video_sizes: tuple[tuple[int, int], ...] = ()
    has_audio: bool = False


def watch_formats(player, seconds):
    """Keep only complete, clear H264/audio profiles, without retaining stream URLs."""
    streaming = player.get("streamingData")
    if not isinstance(streaming, dict) or streaming.get("licenseInfos"):
        return (), False
    sizes, audio = set(), False
    for name in ("formats", "adaptiveFormats"):
        formats = streaming.get(name, [])
        if not isinstance(formats, list):
            continue
        for item in formats[:300]:
            if not isinstance(item, dict) or item.get("drmFamilies") or item.get("drmTrackType"):
                continue
            duration = str(item.get("approxDurationMs", ""))
            mime = item.get("mimeType", "")
            if (
                not isinstance(mime, str)
                or not re.fullmatch(r"[0-9]{1,13}", duration)
                or abs(int(duration) / 1000 - seconds) > max(2, seconds * 0.02)
            ):
                continue
            if mime.startswith("audio/") and ("mp4a." in mime or "opus" in mime):
                audio = True
            width, height = item.get("width"), item.get("height")
            if (
                mime.startswith("video/mp4;")
                and "avc1." in mime
                and type(width) is int
                and type(height) is int
                and 0 < width <= 16384
                and 0 < height <= 16384
            ):
                sizes.add((width, height))
                audio = audio or bool(item.get("audioQuality"))
    return tuple(sorted(sizes)), audio


def watch_metadata(text, identity):
    """Accept only the requested VOD's complete initial player metadata."""
    pattern = r'(?:\bytInitialPlayerResponse\s*=|\["ytInitialPlayerResponse"\]\s*=)\s*'
    for match in islice(re.finditer(pattern, text), 8):
        try:
            player, _ = json.JSONDecoder().raw_decode(text, match.end())
        except (ValueError, RecursionError):
            continue
        if not isinstance(player, dict):
            continue
        details = player.get("videoDetails")
        if not isinstance(details, dict) or details.get("videoId") != identity:
            continue
        length, title, author = (
            details.get("lengthSeconds"),
            details.get("title"),
            details.get("author"),
        )
        if (
            not isinstance(length, str)
            or not re.fullmatch(r"[0-9]{1,7}", length)
            or int(length) <= 0
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(author, str)
            or not author.strip()
            or details.get("isPrivate")
            or details.get("isLiveContent")
        ):
            continue
        status = player.get("playabilityStatus")
        if not isinstance(status, dict):
            continue
        playable = status.get("status") == "OK"
        sizes, audio = watch_formats(player, int(length)) if playable else ((), False)
        return WatchMetadata(title[:500], author[:100], int(length), playable, sizes, audio)
    return None


def public_metadata(payload, identity):
    items = payload.get("items", []) if isinstance(payload, dict) else []
    for item in items[:200] if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("id") != identity:
            continue
        parts = str(item.get("duration", "")).split(":")
        if len(parts) not in (2, 3) or any(not p.isdigit() or len(p) > 3 for p in parts):
            break
        values = list(map(int, parts))
        if values[-1] >= 60 or (len(values) == 3 and values[-2] >= 60):
            break
        seconds = sum(value * 60**index for index, value in enumerate(reversed(values)))
        if seconds > 0:
            return (
                str(item.get("title") or identity)[:500],
                str(item.get("channelTitle") or "YouTube")[:100],
                seconds,
            )
    raise DownloadError(
        "اطلاعات کامل همین ویدیو در مسیر عمومی پیدا نشد.",
        code="youtube_public_metadata_unavailable",
    )


def public_catalog_quality(result, metadata, kind, value):
    """Cross-check the converter's actual rendition against the exact origin catalog."""
    if not metadata or not metadata.playable or not metadata.has_audio:
        return None
    filename = result.get("filename")
    if (
        not isinstance(filename, str)
        or len(filename) > 2048
        or any(ord(char) < 32 for char in filename)
    ):
        return None
    if kind == "mp3" and filename.endswith(".mp3"):
        return public_quality(kind, value, metadata.duration)
    match = re.search(r" \(([0-9]{2,4})p, h264\)\.mp4\Z", filename)
    if kind != "mp4" or not match:
        return None
    height = int(match[1])
    sizes = [(w, h) for w, h in metadata.video_sizes if h == height]
    if len(sizes) != 1 or height > int(value):
        return None
    return public_quality(kind, value, metadata.duration, width=sizes[0][0], height=height)


def public_quality(kind, value, seconds, *, width=None, height=None):
    if kind == "mp3":
        return Quality(
            f"public_audio_mp3_{value}",
            f"MP3 · {value}kbps",
            "mp3",
            int(value),
            "mp3",
            "audio/mpeg",
            "progressive",
            f"public:mp3:{value}",
            duration=seconds,
        )
    return Quality(
        f"public_video_{height}p_h264_mp4",
        f"{height}p · H264 · MP4",
        "h264",
        None,
        "mp4",
        "video/mp4",
        "progressive",
        f"public:mp4:{value}",
        width=width,
        height=height,
        duration=seconds,
    )


def public_download_url(value, identity):
    if not isinstance(value, str) or len(value) > 16384:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme == "https"
            and not parsed.port
            and not parsed.username
            and not parsed.password
            and (host == "yt-dl.click" or host.endswith(".yt-dl.click"))
            and parse_qs(parsed.query).get("vid") == [identity]
        ):
            return value
    except ValueError:
        pass
    return None


def public_video_info(data):
    """Read this provider's bounded MP4 initialization header, including fMP4."""

    def boxes(blob):
        offset = 0
        while offset + 8 <= len(blob):
            size = int.from_bytes(blob[offset : offset + 4], "big")
            if size < 8 or offset + size > len(blob):
                return
            yield bytes(blob[offset + 4 : offset + 8]), blob[offset + 8 : offset + size]
            offset += size

    def child(blob, tag):
        return next((value for name, value in boxes(blob) if name == tag), b"")

    movie = child(data, b"moov")
    width = height = 0
    audio, codec = False, ""
    for tag, track in boxes(movie):
        if tag != b"trak":
            continue
        mdia = child(track, b"mdia")
        handler = child(mdia, b"hdlr")[8:12]
        if handler == b"soun":
            audio = True
        if handler != b"vide":
            continue
        tkhd = child(track, b"tkhd")
        if len(tkhd) < 84:
            continue
        width = int.from_bytes(tkhd[-8:-4], "big") >> 16
        height = int.from_bytes(tkhd[-4:], "big") >> 16
        if tkhd[-44:-40] == bytes(4) and tkhd[-28:-24] == bytes(4):
            width, height = height, width
        stsd = child(child(child(mdia, b"minf"), b"stbl"), b"stsd")
        sample = next(boxes(stsd[8:]), None)
        if sample and sample[0] in {b"avc1", b"avc3"}:
            codec = "h264"
    return width, height, audio, codec


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
            chunk_size=HTTP_CHUNK_SIZE if not is_hls(self.video) else None,
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
