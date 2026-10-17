"""Cross-provider input audit; each provider still owns its URL implementation."""

from urllib.parse import quote

import pytest

from downloader_bot.downloaders.instagram.parser import content_path as ig_path
from downloader_bot.downloaders.instagram.urls import PAGE_HOSTS as IG_HOSTS
from downloader_bot.downloaders.pinterest.parser import pin_id
from downloader_bot.downloaders.pinterest.urls import PINTEREST_HOSTS as PIN_HOSTS
from downloader_bot.downloaders.router import SITE_EXTRACTORS, DownloaderRouter
from downloader_bot.downloaders.soundcloud.urls import normalize_url as sc_url
from downloader_bot.downloaders.spotify.urls import PAGE_HOSTS as SP_HOSTS
from downloader_bot.downloaders.spotify.urls import content_id as sp_id
from downloader_bot.downloaders.tiktok.urls import PAGE_HOSTS as TT_HOSTS
from downloader_bot.downloaders.tiktok.urls import content_id as tt_id
from downloader_bot.downloaders.xnxx.parser import video_id as xn_id
from downloader_bot.downloaders.xnxx.urls import PAGE_HOSTS as XN_HOSTS
from downloader_bot.downloaders.xvideos.urls import PAGE_HOSTS as XV_HOSTS
from downloader_bot.downloaders.xvideos.urls import video_id as xv_id
from downloader_bot.downloaders.youtube.parser import video_id as yt_id
from downloader_bot.downloaders.youtube.urls import PAGE_HOSTS as YT_HOSTS
from schemas.media import DownloadError

YT = "jNQXAC9IVRw"
SP = "0VjIjW4GlUZAMYd2vXMi3b"
TT = "6718335390845095173"
PIN = "123456789012345"
CASES = []
for host in ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"):
    CASES.append(
        (
            "soundcloud",
            f"https://{host}/artist/track?si=sharing",
            "https://soundcloud.com/artist/track?si=sharing",
        )
    )
for host in sorted(YT_HOSTS):
    path = YT if host.endswith("youtu.be") else f"watch?v={YT}"
    CASES.append(("youtube", f"https://{host}/{path}", YT))
for path in (
    f"watch/?v={YT}",
    f"?v={YT}",
    f"watch_popup?v={YT}",
    f"watch.php?v={YT}",
    f"movie?v={YT}",
    f"e/{YT}",
    f"v/{YT}",
    f"shorts/{YT}",
    f"live/{YT}",
    f"embed/{YT}",
    f"#/watch?v={YT}",
    "attribution_link?u=" + quote(f"/watch?v={YT}&feature=share", safe=""),
    "redirect?q=" + quote(f"https://youtu.be/{YT}", safe=""),
    "oembed?url=" + quote(f"https://youtu.be/{YT}", safe=""),
):
    CASES.append(("youtube", "https://www.youtube.com/" + path, YT))
for host in sorted(IG_HOSTS):
    for path in (
        "p/ABC123/",
        "reel/ABC123/",
        "reels/ABC123/",
        "tv/ABC123/",
        "username/reel/ABC123/",
        "p/ABC123/embed/",
        "p/ABC123/embed/captioned/",
    ):
        CASES.append(("instagram", f"https://{host}/{path}?igsh=share", ("post", "ABC123")))
for host in sorted(SP_HOSTS):
    for path in (
        f"track/{SP}",
        f"intl-en/track/{SP}",
        f"intl-pt-br/track/{SP}",
        f"embed/track/{SP}",
    ):
        CASES.append(("spotify", f"https://{host}/{path}?si=sharing", SP))
for host in sorted(TT_HOSTS):
    for path in (
        f"@tester/video/{TT}",
        f"@tester/photo/{TT}",
        f"share/video/{TT}",
        f"embed/{TT}",
        f"embed/v2/{TT}",
        f"player/v1/{TT}",
    ):
        CASES.append(("tiktok", f"https://{host}/{path}?sender=sharing", TT))
for host in sorted(PIN_HOSTS):
    CASES.append(("pinterest", f"https://{host}/pin/{PIN}/?invite_code=sharing", PIN))
for path in (f"pin/a-descriptive-title--{PIN}/", f"pin/{PIN}/sent/"):
    CASES.append(("pinterest", "https://www.pinterest.com/" + path, PIN))
for host in sorted(XV_HOSTS - {"static-hw.xvideos.com"}):
    for path, identity in (
        ("video.abc123/title", "abc123"),
        ("video-abc123/title", "abc123"),
        ("video123456/title", "123456"),
        ("embedframe/abc123", "abc123"),
        ("profiles/tester#quickies/a/abc123", "abc123"),
    ):
        CASES.append(("xvideos", f"https://{host}/{path}", identity))
for host in sorted(XN_HOSTS):
    for path in ("video-abc123/title", "video.abc123/title", "videoabc123/title", "video-abc123/"):
        CASES.append(("xnxx", f"https://{host}/{path}", "abc123"))

IDENTITIES = dict(
    soundcloud=sc_url,
    youtube=yt_id,
    instagram=ig_path,
    spotify=sp_id,
    tiktok=tt_id,
    pinterest=pin_id,
    xvideos=xv_id,
    xnxx=xn_id,
)
ROUTER = DownloaderRouter({site: object() for site in SITE_EXTRACTORS})


@pytest.mark.parametrize("site,url,identity", CASES)
def test_supported_web_mobile_country_share_and_embed_variants_route_to_same_identity(
    site, url, identity
):
    extracted = SITE_EXTRACTORS[site]("لینک: (" + url + ").")
    assert extracted
    assert ROUTER.site_for(extracted) == site
    assert IDENTITIES[site](extracted) == identity


@pytest.mark.parametrize(
    "site,url",
    [
        ("soundcloud", "https://on.soundcloud.com/Abc123"),
        ("spotify", "https://spotify.link/Abc123"),
        ("spotify", "https://spoti.fi/Abc123"),
        ("tiktok", "https://vm.tiktok.com/Abc123/"),
        ("tiktok", "https://vt.tiktok.com/Abc123/"),
        ("tiktok", "https://www.tiktok.com/t/Abc123/"),
        ("pinterest", "https://pin.it/Abc123"),
    ],
)
def test_short_links_still_enter_the_owning_provider(site, url):
    assert ROUTER.site_for(url) == site


@pytest.mark.parametrize(
    "site,host",
    [
        ("soundcloud", "soundcloud.com"),
        ("youtube", "youtube.com"),
        ("instagram", "instagram.com"),
        ("spotify", "open.spotify.com"),
        ("tiktok", "www.tiktok.com"),
        ("pinterest", "pinterest.com"),
        ("xvideos", "xvideos.com"),
        ("xnxx", "xnxx.com"),
    ],
)
@pytest.mark.parametrize(
    "pattern",
    [
        "https://{host}.evil.example/a/b",
        "https://evil.{host}/a/b",
        "https://{host}@evil.example/a/b",
        "https://user@{host}/a/b",
        "https://{host}:8443/a/b",
        "ftp://{host}/a/b",
        "https://evil.example/{host}/a/b",
        "me@{host}",
    ],
)
def test_alias_expansion_keeps_host_scheme_and_credential_boundaries(site, host, pattern):
    assert SITE_EXTRACTORS[site](pattern.format(host=host)) is None


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/watch?v=" + YT,
        "https://youtube.com@127.0.0.1/watch?v=" + YT,
        "file:///etc/passwd",
        "https://youtube.com:8443/watch?v=" + YT,
    ],
)
def test_youtube_share_wrapper_revalidates_nested_destination(target):
    with pytest.raises(DownloadError):
        yt_id("https://www.youtube.com/attribution_link?u=" + quote(target, safe=""))
