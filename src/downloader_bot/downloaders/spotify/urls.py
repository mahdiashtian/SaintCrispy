"""Spotify-owned input identities and matched-audio network boundaries."""

import ipaddress
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from schemas.media import DownloadError

DEFAULT_RESOLVER = "https://cnv.cx"
RESOLVER_ORIGIN = "https://iframe.y2meta-uk.com"
TRACK_ID = re.compile(r"[A-Za-z0-9]{22}\Z", re.ASCII)
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z", re.ASCII)
SHORT_HOSTS = {"spotify.link", "spoti.fi"}
PAGE_HOSTS = {"open.spotify.com", "play.spotify.com"}
TRACK_PATH = re.compile(
    r"/(?:intl-[a-z]{2}(?:-[a-z]{2})?/|embed/)?track/([A-Za-z0-9]{22})/?\Z", re.I
)
LINK_PATTERN = re.compile(
    r"(?<![\w.@/:-])(?:https?://)?(?:open\.spotify\.com|play\.spotify\.com|spotify\.link|spoti\.fi)"
    r"(?=$|[/\s?#])(?:[/?#][^\s<>\"]*)?",
    re.I,
)


def https_parts(value):
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
        ):
            raise ValueError()
        return parts
    except (TypeError, ValueError):
        raise DownloadError("لینک اسپاتیفای معتبر نیست.", code="spotify_invalid_url") from None


def normalize_url(value):
    parts = https_parts(value)
    host = (parts.hostname or "").lower()
    if host in PAGE_HOSTS and (match := TRACK_PATH.fullmatch(parts.path)):
        return f"https://open.spotify.com/track/{match[1]}"
    if host in SHORT_HOSTS and re.fullmatch(r"/[A-Za-z0-9_-]{3,100}/?", parts.path):
        return urlunsplit(("https", host, parts.path.rstrip("/"), "", ""))
    raise DownloadError(
        "فعلاً لینک خود آهنگ اسپاتیفای (/track/) رو بفرست؛ آلبوم، پلی‌لیست و پادکست پشتیبانی نمی‌شن.",
        code="spotify_unsupported_link",
    )


def content_id(value):
    parts = urlsplit(normalize_url(value))
    match = TRACK_PATH.fullmatch(parts.path)
    return match[1] if match else None


def extract_url(text):
    if not (match := LINK_PATTERN.search(text)):
        return None
    value = match[0].rstrip(".,;!?)]}»")
    if not re.match(r"https?://", value, re.I):
        value = "https://" + value
    value = re.sub(r"^http://", "https://", value, flags=re.I)
    # Keep recognized unsupported paths so the handler can explain the limitation.
    try:
        parts = https_parts(value)
        return value if (parts.hostname or "").lower() in PAGE_HOSTS | SHORT_HOSTS else None
    except DownloadError:
        return None


def resolver_url(value):
    if not value:
        return ""
    parts = https_parts(value)
    if not parts.hostname or parts.query or parts.fragment or parts.hostname.lower() == "localhost":
        raise ValueError("Invalid Spotify resolver")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Resolver must use a public address")
    return value.rstrip("/")


def download_url(value, identity):
    if not isinstance(value, str) or len(value) > 16384:
        return None
    try:
        parts = https_parts(value)
    except DownloadError:
        return None
    host = (parts.hostname or "").lower()
    if (host == "yt-dl.click" or host.endswith(".yt-dl.click")) and parse_qs(parts.query).get(
        "vid"
    ) == [identity]:
        return value
    return None
