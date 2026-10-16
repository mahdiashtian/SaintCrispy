"""Strict Spotify identity, recording matching and public MP3 response parsing."""

import json
import math
import re
import unicodedata

from schemas.media import DownloadError, Media, Quality

from .urls import TRACK_ID, VIDEO_ID, download_url, https_parts

QUALITY_KEY = "matched_mp3_128"


def read_track(html, identity):
    match = re.search(
        r"<script\b[^>]*\bid=[\"\x27]__NEXT_DATA__[\"\x27][^>]*>(.*?)</script\s*>",
        html,
        re.S | re.I,
    )
    try:
        data = json.loads(match[1]) if match else None
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        if (
            not TRACK_ID.fullmatch(identity)
            or not isinstance(entity, dict)
            or entity.get("type") != "track"
            or entity.get("id") != identity
            or entity.get("uri") != f"spotify:track:{identity}"
        ):
            raise ValueError()
        title = entity.get("name") or entity.get("title")
        artists = entity.get("artists")
        names = [
            a["name"]
            for a in artists
            if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"].strip()
        ]
        millis = entity.get("duration")
        if (
            not isinstance(title, str)
            or not title.strip()
            or not names
            or type(millis) not in (int, float)
            or not math.isfinite(millis)
            or not 1000 <= millis <= 86400000
        ):
            raise ValueError()
        cover = None
        visual = entity.get("visualIdentity")
        images = visual.get("image", []) if isinstance(visual, dict) else []
        for image in images if isinstance(images, list) else []:
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                continue
            parts = https_parts(image["url"])
            if parts.hostname == "i.scdn.co":
                cover = image["url"]
                break
        duration = round(millis / 1000)
        return Media(
            "spotify",
            identity,
            title.strip()[:500],
            ", ".join(names)[:300],
            duration,
            f"https://open.spotify.com/track/{identity}",
            cover,
            (
                Quality(
                    QUALITY_KEY,
                    "MP3 · 128 kbps · نسخهٔ متناظر",
                    "mp3",
                    128,
                    "mp3",
                    "audio/mpeg",
                    "progressive",
                    "matched:mp3:128",
                    duration=duration,
                ),
            ),
        )
    except (ValueError, TypeError, KeyError, IndexError, DownloadError):
        raise DownloadError(
            "اطلاعات کامل همین آهنگ اسپاتیفای پیدا نشد.", code="spotify_metadata_unavailable"
        ) from None


def normalized(value):
    value = "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c)
    )
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


def versions(title):
    words = set(normalized(title).split())
    return words & {
        "live",
        "remix",
        "cover",
        "instrumental",
        "karaoke",
        "acoustic",
        "slowed",
        "sped",
        "remastered",
        "remaster",
        "mashup",
        "mix",
        "edit",
        "nightcore",
        "rework",
    }


def match_recording(payload, media):
    entries = payload.get("entries") if isinstance(payload, dict) else None
    candidates = []
    title = normalized(media.title)
    artist = normalized(media.artist.split(", ", 1)[0])
    if not title or not artist:
        raise DownloadError(
            "نام آهنگ یا هنرمند برای تطبیق کافی نیست.", code="spotify_no_matching_audio"
        )
    for entry in entries[:10] if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        identity, duration = entry.get("id"), entry.get("duration")
        candidate = entry.get("title")
        channel = entry.get("channel") or entry.get("uploader") or ""
        if (
            not isinstance(identity, str)
            or not VIDEO_ID.fullmatch(identity)
            or type(duration) not in (int, float)
            or not math.isfinite(duration)
            or abs(duration - media.duration) > 2
            or not isinstance(candidate, str)
            or not isinstance(channel, str)
            or entry.get("is_live")
            or entry.get("live_status") in {"is_live", "is_upcoming"}
        ):
            continue
        cleaned = normalized(candidate)
        title_words, artist_words = set(title.split()), set(artist.split())
        if not title_words <= set(cleaned.split()) or not artist_words <= set(
            normalized(candidate + " " + channel).split()
        ):
            continue
        without_artist = cleaned.removeprefix(artist).strip()
        if versions(without_artist) != versions(title):
            continue
        official = normalized(channel) in {
            artist,
            artist + " topic",
            artist.replace(" ", "") + "vevo",
        }
        score = (int(official), int("official audio" in cleaned), -abs(duration - media.duration))
        candidates.append((score, identity, float(duration)))
    if not candidates:
        raise DownloadError(
            "نسخهٔ کامل و متناظر این آهنگ با هنرمند و مدت درست پیدا نشد.",
            code="spotify_no_matching_audio",
        )
    _, identity, duration = max(candidates)
    return identity, duration


def conversion_url(payload, identity):
    if isinstance(payload, dict) and payload.get("status") in {"tunnel", "redirect"}:
        target = download_url(payload.get("url"), identity)
        filename = payload.get("filename")
        if target and isinstance(filename, str) and filename.lower().endswith(".mp3"):
            return target
    raise DownloadError(
        "تبدیل نسخهٔ متناظر آهنگ انجام نشد؛ کمی بعد امتحان کن.", code="spotify_conversion_failed"
    )


def is_mp3(sample):
    return sample.startswith(b"ID3") or any(
        sample[i] == 255 and sample[i + 1] & 224 == 224 for i in range(min(1024, len(sample) - 1))
    )


def read_object(body):
    try:
        value = json.loads(body)
        if isinstance(value, dict):
            return value
    except (ValueError, UnicodeError):
        pass
    raise DownloadError("پاسخ مسیر صوتی اسپاتیفای معتبر نیست.", code="spotify_invalid_response")
