"""TikTok-owned input identity and CDN boundaries."""

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from schemas.media import DownloadError

DEFAULT_RESOLVER = "https://www.tikwm.com/api/"
PAGE_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "www.tiktokv.com",
}
CDN_DOMAINS = (
    "tiktok.com",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokcdn-eu.com",
    "tiktokv.com",
    "byteoversea.com",
    "ibytedtos.com",
    "ibyteimg.com",
    "byteimg.com",
    "muscdn.com",
    "tikwm.com",
)
POST_PATH = re.compile(r"/@[\w.\-]{1,128}/(video|photo)/([0-9]{10,25})/?$", re.ASCII)
EMBED_PATH = re.compile(r"/(?:embed(?:/v2)?|player/v1|share/video)/([0-9]{10,25})/?$")
SHARE_PATH = re.compile(r"/(?:t/)?[A-Za-z0-9_-]{3,100}/?$")
LINK_PATTERN = re.compile(
    r"(?<![\w.@/:-])(?:https?://)?(?:"
    + "|".join(re.escape(host) for host in sorted(PAGE_HOSTS))
    + r")(?=$|[/\s?#])(?:[/?#][^\s<>\"]*)?",
    re.IGNORECASE,
)


def _parts(url):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
            or "\\" in url
            or any(character.isspace() or ord(character) < 32 for character in url)
        ):
            raise ValueError()
        return parts
    except (TypeError, ValueError):
        raise DownloadError("لینک تیک‌تاک معتبر نیست.", code="tiktok_invalid_url") from None


def normalize_url(url: str) -> str:
    parts = _parts(url)
    host = (parts.hostname or "").lower()
    if host not in PAGE_HOSTS:
        raise DownloadError("این دامنه مربوط به تیک‌تاک نیست.", code="tiktok_invalid_url")
    if match := POST_PATH.fullmatch(parts.path):
        return urlunsplit(("https", "www.tiktok.com", parts.path.rstrip("/"), "", ""))
    if match := EMBED_PATH.fullmatch(parts.path):
        return f"https://www.tiktok.com/@_/video/{match[1]}"
    if SHARE_PATH.fullmatch(parts.path) and (
        host in {"vm.tiktok.com", "vt.tiktok.com"} or parts.path.startswith("/t/")
    ):
        return urlunsplit(("https", host, parts.path.rstrip("/") + "/", "", ""))
    raise DownloadError("لینک یک ویدیو یا پست عکس تیک‌تاک را بفرست.", code="tiktok_invalid_url")


def content_id(url: str) -> str | None:
    match = POST_PATH.fullmatch(urlsplit(normalize_url(url)).path)
    return match[2] if match else None


def canonical_url(identity: str, author: str, *, photo=False) -> str:
    if not re.fullmatch(r"[0-9]{10,25}", identity):
        raise DownloadError("شناسهٔ پست تیک‌تاک معتبر نیست.", code="tiktok_invalid_identity")
    author = author if re.fullmatch(r"[\w.\-]{1,128}", author, re.ASCII) else "_"
    return f"https://www.tiktok.com/@{author}/{'photo' if photo else 'video'}/{identity}"


def media_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    try:
        parts = _parts(value)
    except DownloadError:
        return None
    host = (parts.hostname or "").lower()
    if host in PAGE_HOSTS or not any(host == d or host.endswith("." + d) for d in CDN_DOMAINS):
        return None
    return value if not parts.fragment else urlunsplit(parts._replace(fragment=""))


def resolver_url(value: str) -> str:
    if not value:
        return ""
    parts = _parts(value)
    if not parts.hostname or parts.query or parts.fragment or parts.hostname == "localhost":
        raise ValueError("Invalid resolver endpoint")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Resolver must use a public endpoint")
    return value


def extract_url(text: str) -> str | None:
    if not (match := LINK_PATTERN.search(text)):
        return None
    value = match[0].rstrip(".,;!?)]}»")
    if not re.match(r"https?://", value, re.IGNORECASE):
        value = "https://" + value
    value = re.sub(r"^http://", "https://", value, flags=re.IGNORECASE)
    try:
        return normalize_url(value)
    except DownloadError:
        return None
