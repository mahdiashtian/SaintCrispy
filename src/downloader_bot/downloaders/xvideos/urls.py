import re
from urllib.parse import urlsplit, urlunsplit

from downloader_bot.schemas.media import DownloadError

PAGE_HOSTS = {
    prefix + domain
    for domain in ("xvideos.com", "xvideos2.com")
    for prefix in ("", "www.", "m.", "fr.", "de.", "it.")
} | {"xvideos.es", "www.xvideos.es", "flashservice.xvideos.com"}


LINK_PATTERN = re.compile(
    r"(?<![\w.@/:-])(?:https?://)?(?:"
    + "|".join(re.escape(host) for host in sorted(PAGE_HOSTS))
    + r")(?=$|[/\s?#])(?:[/?#][^\s<>\"]*)?",
    re.IGNORECASE,
)


def extract_url(text: str) -> str | None:
    match = LINK_PATTERN.search(text)
    if not match:
        return None
    url = match[0].rstrip(".,;!?)]}»")
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url
    return re.sub(r"^http://", "https://", url, flags=re.IGNORECASE)


def validate_page_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname in PAGE_HOSTS
            and parts.username is None
            and parts.password is None
            and parts.port in (None, 443)
            and "\\" not in url
            and not any(ord(character) < 32 or character.isspace() for character in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise DownloadError("لینک معتبر HTTPS از XVideos بفرست.")
    return url


def video_id(url: str) -> str:
    validate_page_url(url)
    parts = urlsplit(url)
    match = re.fullmatch(
        r"/(?:video[.-]([a-z0-9]+)|video(\d+)|embedframe/([a-z0-9]+))(?:/[^?#]*)?",
        parts.path,
        re.IGNORECASE,
    )
    if match:
        return next(value for value in match.groups() if value).lower()
    if re.fullmatch(r"/(?:profiles/|amateur-channels/)?[^/]+/?", parts.path):
        if match := re.fullmatch(r"quickies/a/([a-z0-9]+)", parts.fragment, re.IGNORECASE):
            return match[1].lower()
    raise DownloadError("لینک صفحه یک ویدیو از XVideos را بفرست.")


def normalize_video_url(url: str) -> str:
    identity = video_id(url)
    parts = urlsplit(url)
    if parts.path.lower().startswith("/embedframe/") or parts.fragment.lower().startswith(
        "quickies/a/"
    ):
        domain = (
            "xvideos2.com"
            if parts.hostname.endswith("xvideos2.com")
            else ("xvideos.es" if parts.hostname.endswith("xvideos.es") else "xvideos.com")
        )
        path = f"/video{'' if identity.isdecimal() else '.'}{identity}/_"
        return urlunsplit(("https", "www." + domain, path, "", ""))
    return url
