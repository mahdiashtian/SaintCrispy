import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from schemas.media import DownloadError

WEB_HOSTS = {"www.soundcloud.com", "m.soundcloud.com", "soundcloud.com"}
WIDGET_HOSTS = {"w.soundcloud.com", "player.soundcloud.com", "p.soundcloud.com"}
API_HOSTS = {"api.soundcloud.com", "api-v2.soundcloud.com"}
PAGE_HOSTS = WEB_HOSTS | WIDGET_HOSTS | API_HOSTS | {"on.soundcloud.com"}
TRACK_PATH = re.compile(r"/tracks/(?:soundcloud%3Atracks%3A)?([0-9]{1,20})/?\Z", re.I)


def normalize_url(url: str) -> str:
    for _ in range(3):
        try:
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or parts.hostname not in PAGE_HOSTS
                or parts.username
                or parts.password
                or parts.port not in (None, 443)
                or "\\" in url
                or any(character.isspace() or ord(character) < 32 for character in url)
            ):
                raise ValueError()
            query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=64)
            if parts.hostname in WIDGET_HOSTS:
                if parts.path.rstrip("/") != "/player" or len(query.get("url", [])) != 1:
                    raise ValueError()
                url = query["url"][0]
                # Old official widgets used HTTP targets; validation still precedes any request.
                url = re.sub(r"^http://", "https://", url, flags=re.I)
                if query.get("secret_token"):
                    if len(query["secret_token"]) != 1:
                        raise ValueError()
                    target = urlsplit(url)
                    values = parse_qs(target.query, keep_blank_values=True, max_num_fields=64)
                    values["secret_token"] = query["secret_token"]
                    url = urlunsplit(target._replace(query=urlencode(values, doseq=True)))
                continue
            if parts.hostname in API_HOSTS:
                match = TRACK_PATH.fullmatch(parts.path)
                if not match:
                    raise ValueError()
                return urlunsplit(
                    ("https", "api-v2.soundcloud.com", f"/tracks/{match[1]}", parts.query, "")
                )
            host = "soundcloud.com" if parts.hostname in WEB_HOSTS else parts.hostname
            return urlunsplit(("https", host, parts.path, parts.query, ""))
        except (ValueError, TypeError):
            break
    raise DownloadError("لینک معتبر HTTPS از SoundCloud بفرست.", code="soundcloud_invalid_url")


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
    try:
        return normalize_url(re.sub(r"^http://", "https://", url, flags=re.IGNORECASE))
    except DownloadError:
        return None


def cache_alias(url: str) -> str:
    parts = urlsplit(normalize_url(url))
    host = parts.hostname
    # Slugs are aliases, never track IDs. Preserve private access tokens.
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key not in {"si", "ref", "in"}
    ]
    return urlunsplit(("https", host, parts.path.rstrip("/"), urlencode(sorted(query)), ""))
