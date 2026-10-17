"""SoundCloud mobile, short, widget and API aliases keep one track identity."""

from urllib.parse import quote

import httpx
import pytest
from test_client import client_page
from test_parser import variant
from test_soundcloud_downloader import track_page

from downloader_bot.downloaders.soundcloud.client import SoundCloudClient
from downloader_bot.downloaders.soundcloud.downloader import SoundCloudDownloader
from downloader_bot.downloaders.soundcloud.urls import cache_alias, extract_url, normalize_url
from schemas.media import DownloadError, SiteHTTPError

PAGE = "https://soundcloud.com/artist/track"


@pytest.mark.parametrize("host", ["soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"])
async def test_web_aliases_use_desktop_html_and_produce_the_same_track(host):
    body, _ = track_page([variant("mp3_1_0", "progressive")])
    visited = []

    def respond(request):
        visited.append(request.url.host)
        if request.url.host == "soundcloud.com":
            return httpx.Response(200, text=body)
        return httpx.Response(200, json={"url": "https://cdn.example/full.mp3"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        d = SoundCloudDownloader(SoundCloudClient(http))
        m = await d.inspect(f"https://{host}/artist/track?si=tracking")
        assert m.content_id == "123" and len(m.qualities) == 1
        assert d.cache_key(f"https://{host}/artist/track") == cache_alias(PAGE)
    assert "m.soundcloud.com" not in visited and "www.soundcloud.com" not in visited


async def test_short_redirect_to_mobile_is_normalized_before_request():
    visited = []

    def respond(request):
        visited.append(str(request.url))
        if request.url.host == "on.soundcloud.com":
            return httpx.Response(
                302, headers={"location": "https://m.soundcloud.com/artist/track"}
            )
        return httpx.Response(200, text="desktop")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await SoundCloudClient(http).page("https://on.soundcloud.com/abc") == (
            "desktop",
            PAGE,
        )
    assert visited == ["https://on.soundcloud.com/abc", PAGE]


async def test_track_page_without_bootstrap_uses_one_desktop_home_lookup():
    visited = []

    def respond(request):
        visited.append(str(request.url))
        return httpx.Response(200, text=client_page("a" * 32))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert (
            await SoundCloudClient(http).public_client_id("<html>mobile shell</html>") == "a" * 32
        )
    assert visited == ["https://soundcloud.com/"]


@pytest.mark.parametrize(
    "expression",
    [
        'client_id:"' + "b" * 32 + '"',
        '"client_id":"' + "b" * 32 + '"',
        "client_id='" + "b" * 32 + "'",
    ],
)
async def test_bootstrap_assets_accept_html_quotes_and_protocol_relative_sources(expression):
    visited = []

    def respond(request):
        visited.append(str(request.url))
        return httpx.Response(200, text=expression)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        page = "<script defer src='//a-v2.sndcdn.com/current.js'></script>"
        assert await SoundCloudClient(http).public_client_id(page) == "b" * 32
    assert visited == ["https://a-v2.sndcdn.com/current.js"]


async def test_asset_rate_limit_does_not_trigger_homepage_or_another_asset():
    visited = []

    def respond(request):
        visited.append(str(request.url))
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(SiteHTTPError):
            await SoundCloudClient(http).public_client_id(
                '<script src="https://a-v2.sndcdn.com/current.js"></script>'
            )
    assert len(visited) == 1


async def test_bootstrap_rejects_hostile_scripts_and_does_not_follow_asset_redirects():
    visited = []

    def respond(request):
        visited.append(str(request.url))
        if request.url.host == "a-v2.sndcdn.com":
            return httpx.Response(302, headers={"location": "https://evil.example/asset.js"})
        return httpx.Response(200, text=client_page("c" * 32))

    page = """
    <script src="https://["></script>
    <script src="https://a-v2.sndcdn.com.evil.example/asset.js"></script>
    <script src="https://user@a-v2.sndcdn.com/asset.js"></script>
    <script src="https://a-v2.sndcdn.com/current.js"></script>
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await SoundCloudClient(http).public_client_id(page) == "c" * 32
    assert visited == ["https://a-v2.sndcdn.com/current.js", "https://soundcloud.com/"]


@pytest.mark.parametrize("host", ["w.soundcloud.com", "player.soundcloud.com", "p.soundcloud.com"])
async def test_widget_numeric_track_uses_own_api_and_preserves_supplied_access_token(host):
    page, track = track_page([variant("mp3_1_0", "progressive")])
    seen = []

    def respond(request):
        seen.append(request.url.host)
        if request.url.host == "soundcloud.com":
            return httpx.Response(200, text=page)
        if request.url.path == "/tracks/123":
            assert request.url.params["secret_token"] == "s-example"
            return httpx.Response(200, json=track)
        return httpx.Response(200, json={"url": "https://cdn.example/full.mp3"})

    target = "https://api.soundcloud.com/tracks/123"
    url = f"https://{host}/player/?url={quote(target, safe='')}&secret_token=s-example"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        m = await SoundCloudDownloader(SoundCloudClient(http)).inspect(url)
        assert m.content_id == "123"
    assert set(seen) == {"soundcloud.com", "api-v2.soundcloud.com"}


@pytest.mark.parametrize("response_id", [123, 456])
async def test_numeric_api_requires_matching_identity_and_keeps_token_out_of_page_url(response_id):
    page, track = track_page([variant("mp3_1_0", "progressive")])
    track.pop("permalink_url", None)
    track["id"] = response_id

    def respond(request):
        if request.url.host == "soundcloud.com":
            return httpx.Response(200, text=page)
        if request.url.path == "/tracks/123":
            return httpx.Response(200, json=track)
        return httpx.Response(200, json={"url": "https://cdn.example/full.mp3"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = SoundCloudDownloader(SoundCloudClient(http))
        url = "https://api.soundcloud.com/tracks/123?secret_token=s-example"
        if response_id == 123:
            assert (
                await downloader.inspect(url)
            ).page_url == "https://api-v2.soundcloud.com/tracks/123"
        else:
            with pytest.raises(DownloadError) as caught:
                await downloader.inspect(url)
            assert caught.value.code == "soundcloud_identity_mismatch"


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/tracks/123",
        "https://127.0.0.1/tracks/123",
        "file:///etc/passwd",
        "https://soundcloud.com@evil.example/a/b",
        "https://soundcloud.com:8443/a/b",
        "https://api.soundcloud.com/playlists/123",
    ],
)
async def test_widget_unsafe_or_collection_target_is_rejected_before_io(target):
    url = "https://w.soundcloud.com/player/?url=" + quote(target, safe="")

    def reject(request):
        pytest.fail("A rejected wrapper must not perform network I/O")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        with pytest.raises(DownloadError):
            await SoundCloudDownloader(SoundCloudClient(http)).inspect(url)
    assert extract_url(url) is None


def test_widget_permalink_and_numeric_api_aliases_normalize_without_losing_identity():
    assert normalize_url("https://w.soundcloud.com/player/?url=" + quote(PAGE, safe="")) == PAGE
    assert (
        normalize_url("https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A123")
        == "https://api-v2.soundcloud.com/tracks/123"
    )
