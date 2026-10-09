import re
from urllib.parse import urlsplit, urlunsplit

PAGE_HOSTS = {
    "m.xnxx3.com",
    "xnxx3.com",
    "video.xnxx3.com",
    "xnxx.com",
    "www.xnxx3.com",
    "m.xnxx.com",
    "www.xnxx.com",
    "video.xnxx.com",
}


LINK_PATTERN = re.compile(
    r"(?<![\w.@/:-])(?:https?://)?(?:"
    + "|".join(re.escape(host) for host in sorted(PAGE_HOSTS))
    + r")(?=$|[/\s?#])(?:[/?#][^\s<>\"]*)?",
    re.IGNORECASE,
)


def with_page_slug(url: str) -> str:
    """The public video endpoint returns 404 when the title segment is absent."""
    parts = urlsplit(url)
    if re.fullmatch(r"/video[-.]?[a-z0-9]+/?", parts.path, re.IGNORECASE):
        return urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/video"))
    return url


def extract_url(text: str) -> str | None:
    match = LINK_PATTERN.search(text)
    if not match:
        return None
    url = match[0].rstrip(".,;!?)]}»")
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url
    return re.sub(r"^http://", "https://", url, flags=re.IGNORECASE)
