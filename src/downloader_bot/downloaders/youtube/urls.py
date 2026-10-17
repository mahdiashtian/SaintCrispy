import re

DEFAULT_RESOLVER = "https://cnv.cx"
PUBLIC_METADATA_URL = "https://mw.mp3juice.blog/search.php"
PUBLIC_ORIGIN = "https://iframe.y2meta-uk.com"

PAGE_HOSTS = {
    "youtu.be",
    "youtube-nocookie.com",
    "m.youtube.com",
    "www.youtu.be",
    "www.youtube.com",
    "youtube.com",
    "www.youtube-nocookie.com",
    "music.youtube.com",
    "youtube.googleapis.com",
}


def network_host(host: str) -> bool:
    return host in PAGE_HOSTS or any(
        host == suffix or host.endswith("." + suffix)
        for suffix in (
            "youtube.com",
            "googlevideo.com",
            "googleapis.com",
            "ytimg.com",
            "google.com",
        )
    )


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
