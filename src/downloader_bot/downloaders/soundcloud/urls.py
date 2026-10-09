import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PAGE_HOSTS = {"www.soundcloud.com", "m.soundcloud.com", "soundcloud.com", "on.soundcloud.com"}


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


def cache_alias(url: str) -> str:
    parts = urlsplit(url)
    host = "on.soundcloud.com" if parts.hostname == "on.soundcloud.com" else "soundcloud.com"
    # Slugs are aliases, never track IDs. Preserve private access tokens.
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key not in {"si", "ref", "in"}
    ]
    return urlunsplit(("https", host, parts.path.rstrip("/"), urlencode(sorted(query)), ""))
