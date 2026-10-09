import re

PAGE_HOSTS = {
    "youtu.be",
    "youtube-nocookie.com",
    "m.youtube.com",
    "www.youtu.be",
    "www.youtube.com",
    "youtube.com",
    "www.youtube-nocookie.com",
    "music.youtube.com",
}


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
