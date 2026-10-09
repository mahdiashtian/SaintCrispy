import json
import re
from dataclasses import replace
from urllib.parse import urlsplit

from downloader_bot.schemas.media import DownloadError, Quality

from .urls import PAGE_HOSTS


def validate_page_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in PAGE_HOSTS
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise DownloadError("لینک معتبر HTTPS از SoundCloud بفرست.")
    return url


def read_hydration(page: str) -> list[dict]:
    match = re.search(r"window\.__sc_hydration\s*=\s*", page)
    if not match:
        return []
    try:
        hydration, _ = json.JSONDecoder().raw_decode(page[match.end() :])
    except ValueError:
        return []
    if not isinstance(hydration, list):
        return []
    return [item for item in hydration if isinstance(item, dict)]


def read_track(page: str) -> dict:
    try:
        track = next(
            item["data"] for item in read_hydration(page) if item.get("hydratable") == "sound"
        )
        if not isinstance(track, dict) or not track.get("id"):
            raise ValueError("Missing track")
    except (ValueError, KeyError, StopIteration) as error:
        raise DownloadError("فعلاً لینک یک آهنگ را بفرست؛ لینک مجموعه پشتیبانی نشده است.") from error
    return track


def is_preview_url(url: str, duration: int) -> bool:
    path = urlsplit(url).path
    return "/preview/" in path or (duration > 30 and bool(re.search(r"/playlist/0/30/", path)))


def read_qualities(track: dict) -> tuple[Quality, ...]:
    # A quality identifies the audio, not the delivery protocol or temporary URL.
    qualities: dict[str, Quality] = {}
    for item in transcodings(track):
        preset = item.get("preset")
        endpoint = item.get("url")
        description = item.get("format") or {}
        if not isinstance(preset, str) or not isinstance(endpoint, str):
            continue
        if not isinstance(description, dict):
            continue
        protocol = description.get("protocol")
        if protocol == "encrypted-hls":
            protocol = "hls"  # Standard AES-128 HLS is handled by FFmpeg.
        if (
            item.get("snipped")
            or "/preview/" in endpoint
            or preset.startswith("abr")
            or protocol not in ("hls", "progressive")
        ):
            continue
        codec = preset.split("_", 1)[0]
        if codec not in ("aac", "mp3", "opus"):
            continue
        bitrate_match = re.search(r"_(\d+)k", preset)
        bitrate = int(bitrate_match[1]) if bitrate_match else None
        if bitrate is None and codec == "aac" and item.get("quality") == "hq":
            bitrate = 256
        # Legacy MP3 presets do not specify a bitrate. Do not invent one in the menu.
        quality_tag = str(bitrate) if bitrate else item.get("quality", "sq")
        key = f"{codec}_{quality_tag}"
        label = (
            f"{codec.upper()} {bitrate} kbps"
            if bitrate
            else (
                f"{codec.upper()} کیفیت بالا"
                if quality_tag == "hq"
                else f"{codec.upper()} استاندارد"
            )
        )
        quality = Quality(
            key,
            label,
            codec,
            bitrate,
            {"aac": "m4a", "mp3": "mp3", "opus": "opus"}[codec],
            {"aac": "audio/mp4", "mp3": "audio/mpeg", "opus": "audio/ogg"}[codec],
            protocol,
            endpoint,
        )
        previous = qualities.get(key)
        if previous is None:
            qualities[key] = quality
        elif protocol == "progressive" and previous.protocol == "hls":
            qualities[key] = replace(quality, fallback_endpoint=previous.endpoint)
        elif protocol == "hls" and previous.protocol == "progressive":
            qualities[key] = replace(previous, fallback_endpoint=quality.endpoint)
    return tuple(sorted(qualities.values(), key=lambda q: q.bitrate or 0, reverse=True))


def transcodings(track: dict) -> tuple[dict, ...]:
    media = track.get("media") or {}
    if not isinstance(media, dict):
        return ()
    items = media.get("transcodings") or []
    if not isinstance(items, list):
        return ()
    return tuple(item for item in items if isinstance(item, dict))


def unavailable_error(track: dict) -> DownloadError:
    if any(
        str((item.get("format") or {}).get("protocol", "")).startswith(("ctr-", "cbc-"))
        for item in transcodings(track)
        if isinstance(item.get("format") or {}, dict)
    ):
        return DownloadError(
            "جریان‌های کامل این آهنگ DRM دارند و لینک دانلود معمولی ارائه نمی‌کنند.",
            code="soundcloud_protected_stream",
        )
    if any(item.get("snipped") for item in transcodings(track)):
        return DownloadError(
            "SoundCloud برای دسترسی فعلی فقط پیش‌نمایش آهنگ را ارائه کرده است.",
            code="soundcloud_preview_only",
        )
    return DownloadError(
        "SoundCloud لینک پخش کامل و قابل دریافت برای این آهنگ ارائه نکرد.",
        code="soundcloud_no_full_stream",
    )
