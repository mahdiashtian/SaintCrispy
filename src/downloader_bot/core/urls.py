import re

from downloader_bot.downloaders.instagram.urls import LINK_PATTERN as INSTAGRAM_LINK
from downloader_bot.downloaders.instagram.urls import extract_url as extract_instagram_url
from downloader_bot.downloaders.pinterest.urls import LINK_PATTERN as PINTEREST_LINK
from downloader_bot.downloaders.pinterest.urls import PINTEREST_DOMAINS as PINTEREST_DOMAINS
from downloader_bot.downloaders.pinterest.urls import PINTEREST_HOSTS as PINTEREST_HOSTS
from downloader_bot.downloaders.pinterest.urls import extract_url as extract_pinterest_url
from downloader_bot.downloaders.soundcloud.urls import LINK_PATTERN as SOUNDCLOUD_LINK
from downloader_bot.downloaders.soundcloud.urls import extract_url as extract_soundcloud_url
from downloader_bot.downloaders.xnxx.urls import LINK_PATTERN as XNXX_LINK
from downloader_bot.downloaders.xnxx.urls import extract_url as extract_xnxx_url
from downloader_bot.downloaders.xvideos.urls import LINK_PATTERN as XVIDEOS_LINK
from downloader_bot.downloaders.xvideos.urls import extract_url as extract_xvideos_url
from downloader_bot.downloaders.youtube.urls import LINK_PATTERN as YOUTUBE_LINK
from downloader_bot.downloaders.youtube.urls import extract_url as extract_youtube_url


def site_link_pattern(domain: str, aliases: tuple[str, ...] = ()) -> re.Pattern:
    hosts = [f"(?:www\\.|m\\.)?{re.escape(domain)}"]
    hosts.extend(re.escape(host) for host in aliases)
    # Both prefix and suffix checks prevent accepting lookalike domains.
    return re.compile(
        r"(?<![\w.@/:-])(?:https?://)?(?:"
        + "|".join(hosts)
        + r")(?=$|[/\s?#])(?:[/?#][^\s<>\"]*)?",
        re.IGNORECASE,
    )


SITE_EXTRACTORS = {
    "soundcloud": extract_soundcloud_url,
    "xnxx": extract_xnxx_url,
    "xvideos": extract_xvideos_url,
    "youtube": extract_youtube_url,
    "instagram": extract_instagram_url,
    "pinterest": extract_pinterest_url,
}

__all__ = [
    "SOUNDCLOUD_LINK",
    "extract_soundcloud_url",
    "XNXX_LINK",
    "extract_xnxx_url",
    "XVIDEOS_LINK",
    "extract_xvideos_url",
    "YOUTUBE_LINK",
    "extract_youtube_url",
    "INSTAGRAM_LINK",
    "extract_instagram_url",
    "PINTEREST_LINK",
    "extract_pinterest_url",
    "PINTEREST_DOMAINS",
    "PINTEREST_HOSTS",
    "SITE_EXTRACTORS",
    "site_link_pattern",
]
