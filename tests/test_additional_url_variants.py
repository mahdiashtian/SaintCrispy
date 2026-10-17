"""Exercise additional app/widget inputs through provider discovery and selection."""

from urllib.parse import parse_qs, quote

import httpx
import pytest
from test_pinterest_downloader import PIN, pin_data
from test_spotify_downloader import PAGE as SPOTIFY_PAGE
from test_spotify_downloader import TRACK, page
from test_tiktok_downloader import IDENTITY, resolver_payload
from test_xnxx_downloader import PAGE as XNXX_HTML
from test_xvideos_downloader import MP4
from test_xvideos_downloader import PAGE as XVIDEOS_HTML

from downloader_bot.downloaders.pinterest.client import PinterestClient
from downloader_bot.downloaders.pinterest.downloader import PinterestDownloader
from downloader_bot.downloaders.router import SITE_EXTRACTORS, DownloaderRouter
from downloader_bot.downloaders.spotify.client import SpotifyClient
from downloader_bot.downloaders.spotify.downloader import SpotifyDownloader
from downloader_bot.downloaders.spotify.urls import content_id, normalize_url
from downloader_bot.downloaders.tiktok.client import TikTokClient
from downloader_bot.downloaders.tiktok.downloader import TikTokDownloader
from downloader_bot.downloaders.xnxx.client import XNXXClient
from downloader_bot.downloaders.xnxx.downloader import XNXXDownloader
from downloader_bot.downloaders.xvideos.client import XVideosClient
from downloader_bot.downloaders.xvideos.downloader import XVideosDownloader
from downloader_bot.downloaders.xvideos.urls import normalize_video_url
from downloader_bot.downloaders.youtube.parser import video_id
from schemas.media import DownloadError

SPOTIFY_VARIANTS = [
    f"spotify:track:{TRACK}",
    f"https://embed.spotify.com/?uri=spotify%3Atrack%3A{TRACK}&theme=white",
    f"https://open.spotify.com/embed?uri=spotify:track:{TRACK}",
    "https://open.spotify.com/embed/?uri=" + quote(SPOTIFY_PAGE, safe=""),
]
PINTEREST_VARIANTS = [
    f"https://assets.pinterest.com/ext/embed.html?id={PIN}&height=445",
    f"https://in.pinterest.com/pin/{PIN}/",
    f"https://au.pinterest.com/pin/{PIN}/",
]
NESTED_SPOTIFY = SPOTIFY_PAGE
for _ in range(5):
    NESTED_SPOTIFY = "https://open.spotify.com/embed?uri=" + quote(NESTED_SPOTIFY, safe="")


@pytest.mark.parametrize("url", SPOTIFY_VARIANTS)
async def test_spotify_app_and_legacy_widgets_use_only_canonical_track_metadata(url):
    visited = []

    def respond(request):
        visited.append(str(request.url))
        return httpx.Response(200, text=page())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = SpotifyDownloader(SpotifyClient(http, http))
        router = DownloaderRouter({"spotify": downloader})
        media = await router.inspect(url)
        assert media.content_id == TRACK
        assert router.cache_key(url) == "spotify:" + TRACK
        assert media.page_url == SPOTIFY_PAGE
    assert visited == [f"https://open.spotify.com/embed/track/{TRACK}"]
    assert normalize_url(url) == SPOTIFY_PAGE and content_id(url) == TRACK


@pytest.mark.parametrize("url", PINTEREST_VARIANTS)
async def test_pinterest_widget_and_regional_inputs_discover_and_resolve_the_same_pin(url):
    visited = []
    pin = pin_data(video=False)

    def respond(request):
        visited.append(request.url.host)
        if request.url.path.endswith("/get/"):
            return httpx.Response(200, json={"resource_response": {"data": pin}})
        if request.url.path.startswith("/pin/"):
            return httpx.Response(404)
        assert request.url.host == "i.pinimg.com"
        return httpx.Response(200, content=b"\xff\xd8\xff" + b"\0" * 50)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        router = DownloaderRouter({"pinterest": PinterestDownloader(PinterestClient(http))})
        media = await router.inspect(url)
        source = await router.resolve(media, media.qualities[0])
        assert media.content_id == PIN and source.protocol == "progressive"
        assert router.cache_key(url) == "pinterest:" + PIN
    assert set(visited) <= {"www.pinterest.com", "i.pinimg.com"}


@pytest.mark.parametrize("path", ["share/video", "embed", "embed/v2", "player/v1"])
async def test_tiktok_legacy_app_host_resolves_and_selects_the_requested_post(path):
    def respond(request):
        assert request.url.host == "www.tikwm.com"
        submitted = parse_qs(request.content.decode())["url"][0]
        assert submitted == f"https://www.tiktok.com/@_/video/{IDENTITY}"
        return httpx.Response(200, json=resolver_payload())

    url = f"https://www.tiktokv.com/{path}/{IDENTITY}?share_app_id=1233"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        router = DownloaderRouter({"tiktok": TikTokDownloader(TikTokClient(http))})
        media = await router.inspect(url)
        source = await router.resolve(media, media.qualities[0])
        assert media.content_id == IDENTITY and source.protocol == "progressive"
        assert router.cache_key(url) == "tiktok:" + IDENTITY


@pytest.mark.parametrize("identity", ["demo", "4588838"])
async def test_legacy_xvideos_player_uses_normal_page_without_requesting_a_flash_file(identity):
    visited = []
    canonical = f"https://www.xvideos.com/video{'' if identity.isdecimal() else '.'}{identity}/_"
    html = XVIDEOS_HTML.replace("html5player.setVideoHLS('https://cdn.example/master.m3u8');", "")

    def respond(request):
        visited.append(str(request.url))
        if request.url.host == "www.xvideos.com":
            assert str(request.url) == canonical
            return httpx.Response(200, text=html)
        assert request.url.host == "cdn.example"
        return httpx.Response(200, content=MP4)

    url = f"https://static-hw.xvideos.com/swf/xv-player.swf?id_video={identity}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        router = DownloaderRouter({"xvideos": XVideosDownloader(XVideosClient(http))})
        media = await router.inspect(url)
        source = await router.resolve(media, media.qualities[0])
        assert media.content_id == identity and source.protocol == "progressive"
        assert router.cache_key(url) == "xvideos:" + identity
    assert normalize_video_url(url) == canonical
    assert all("static-hw" not in target and ".swf" not in target for target in visited)


def test_legacy_youtube_google_embed_keeps_the_same_video_identity():
    url = "https://youtube.googleapis.com/v/jNQXAC9IVRw?version=3"
    assert video_id(url) == "jNQXAC9IVRw"
    router = DownloaderRouter({"youtube": object()})
    assert router.site_for(url) == "youtube"


@pytest.mark.parametrize(
    "url",
    [
        "https://m.xnxx.com/video-demo/title",
        "https://video.xnxx.com/videodemo/title",
        "https://www.xnxx3.com/video-demo/",
    ],
)
async def test_existing_xnxx_mobile_and_old_aliases_keep_identity_through_selection(url):
    html = XNXX_HTML.replace("html5player.setVideoHLS('https://cdn.example/master.m3u8');", "")

    def respond(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=MP4)
        return httpx.Response(200, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        router = DownloaderRouter({"xnxx": XNXXDownloader(XNXXClient(http))})
        media = await router.inspect(url)
        source = await router.resolve(media, media.qualities[0])
        assert media.content_id == "demo" and source.protocol == "progressive"
        assert router.cache_key(url) == "xnxx:demo"


@pytest.mark.parametrize(
    "site,url",
    [
        (
            "spotify",
            f"https://embed.spotify.com/?uri=spotify:track:{TRACK}&uri=spotify:track:{TRACK}",
        ),
        ("spotify", "https://embed.spotify.com/?uri=https%3A%2F%2Fevil.example%2Ftrack%2F" + TRACK),
        ("spotify", "https://open.spotify.com/embed?uri=spotify:album:" + TRACK),
        ("spotify", f"https://embed.spotify.com/other?uri=spotify:track:{TRACK}"),
        ("spotify", f"https://open.spotify.com/embed?uri=https://127.0.0.1/track/{TRACK}"),
        ("spotify", NESTED_SPOTIFY),
        ("pinterest", f"https://assets.pinterest.com/ext/embed.html?id={PIN}&id={PIN}"),
        ("pinterest", "https://assets.pinterest.com/ext/embed.html?id=https://evil.example/"),
        ("pinterest", f"https://assets.pinterest.com/ext/other.html?id={PIN}"),
        ("xvideos", "https://static-hw.xvideos.com/swf/xv-player.swf?id_video=demo&id_video=other"),
        (
            "xvideos",
            "https://static-hw.xvideos.com/swf/xv-player.swf?id_video=https://evil.example",
        ),
        ("xvideos", "https://static-hw.xvideos.com/video.demo/title"),
        ("tiktok", "https://www.tiktokv.com.evil.example/share/video/" + IDENTITY),
    ],
)
async def test_new_wrappers_reject_ambiguous_or_foreign_content_before_io(site, url):
    def reject(request):
        pytest.fail("Rejected aliases must not perform network I/O")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        providers = {
            "spotify": SpotifyDownloader(SpotifyClient(http, http)),
            "pinterest": PinterestDownloader(PinterestClient(http)),
            "xvideos": XVideosDownloader(XVideosClient(http)),
            "tiktok": TikTokDownloader(TikTokClient(http)),
        }
        with pytest.raises(DownloadError):
            await DownloaderRouter({site: providers[site]}).inspect(url)


@pytest.mark.parametrize(
    "site,host",
    [
        ("spotify", "embed.spotify.com"),
        ("pinterest", "assets.pinterest.com"),
        ("xvideos", "static-hw.xvideos.com"),
        ("tiktok", "www.tiktokv.com"),
        ("youtube", "youtube.googleapis.com"),
    ],
)
def test_new_alias_hosts_do_not_accept_lookalikes(site, host):
    assert SITE_EXTRACTORS[site](f"https://{host}.evil.example/a/b") is None


@pytest.mark.parametrize(
    "url",
    [
        "spotify:track:" + TRACK[:-1],
        "spotify:track:" + TRACK + "X",
        "spotify:track:" + TRACK + ":extra",
        "file:spotify:track:" + TRACK,
        "https://evil.example/spotify:track:" + TRACK,
    ],
)
def test_spotify_app_uri_must_be_complete_and_outside_another_scheme(url):
    assert SITE_EXTRACTORS["spotify"](url) is None
