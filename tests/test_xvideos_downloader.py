import asyncio
from dataclasses import replace

import httpx
import pytest

from downloader_bot.downloaders.router import DownloaderRouter
from downloader_bot.downloaders.xvideos.client import XVideosClient
from downloader_bot.downloaders.xvideos.downloader import XVideosDownloader
from downloader_bot.downloaders.xvideos.parser import extract_links, read_video
from downloader_bot.models import DownloadError, SiteHTTPError
from downloader_bot.urls import extract_xvideos_url

PAGE_URL = "https://www.xvideos.com/video.demo/example"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"
TS = (b"\x47" + b"\x00" * 187) * 6
PLAYLIST = "#EXTM3U\n#EXTINF:120,\nsegment.ts\n#EXT-X-ENDLIST\n"
PAGE = r"""
<meta property="og:title" content="Demo &amp; Test">
<meta property="og:image" content="//cdn.example/thumb.jpg">
<title>Fallback</title>
html5player.setVideoUrlHigh('https:\/\/cdn.example\/high.mp4?token=first\u0026a=1');
html5player.setVideoUrlLow('//cdn.example/low.mp4');
html5player.setVideoHLS('https://cdn.example/master.m3u8');
html5player.setVideoDuration(120);
"""
MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028",AUDIO="sound"
1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
720.m3u8
"""


@pytest.mark.parametrize(
    ("path", "identity"),
    [
        ("video.demo/title", "demo"),
        ("video12345/title", "12345"),
        ("video-demo/title", "demo"),
    ],
)
def test_player_urls_metadata_and_video_identity(path, identity):
    media = read_video(PAGE, f"https://www.xvideos.com/{path}")
    assert (media.site, media.content_id, media.title) == ("xvideos", identity, "Demo & Test")
    assert media.duration == 120 and media.thumbnail == "https://cdn.example/thumb.jpg"
    assert [item.key for item in media.qualities] == ["high", "hls", "low"]
    assert media.qualities[0].endpoint == "https://cdn.example/high.mp4?token=first&a=1"


def test_literal_media_fallback_from_the_supplied_script_is_preserved():
    page = """<title>Demo / test?</title>
    <source src="https://cdn.example/movie.mp4?token=1&amp;a=2">
    <script>const playlist = 'https://cdn.example/master.m3u8?token=2';</script>
    """
    info = extract_links(page, PAGE_URL)
    assert info["title"] == "Demo test"
    assert info["high"] == "https://cdn.example/movie.mp4?token=1&a=2"
    assert info["hls"] == "https://cdn.example/master.m3u8?token=2"
    assert info["low"] is None


def test_literal_fallback_does_not_replace_a_present_player_quality():
    info = extract_links(
        "setVideoUrlLow('//cdn.example/low.mp4'); 'https://cdn.example/unrelated.mp4'",
        PAGE_URL,
    )
    assert info["low"] == "https://cdn.example/low.mp4" and info["high"] is None


@pytest.mark.parametrize(
    "url",
    [
        "xvideos.com/video.demo/example",
        "ویدیو www.xvideos.com/video.demo/example",
        "(HTTP://M.XVIDEOS.COM/video.demo/example).",
    ],
)
def test_domain_links_are_normalized(url):
    assert extract_xvideos_url(url).lower().startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "https://xvideos.com.evil.org/video.demo",
        "https://evil.xvideos.com/video.demo",
        "https://notxvideos.com/video.demo",
        "https://xvideos.com@evil.org/video.demo",
        "https://evil.org/xvideos.com/video.demo",
        "https://xvideos.com:9999/video.demo",
        "me@xvideos.com",
        "ftp://xvideos.com/video.demo",
    ],
)
def test_lookalike_domains_are_not_routed(url):
    assert extract_xvideos_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.xvideos.com/",
        "https://www.xvideos.com/search/demo",
        "https://www.xnxx.com/video-demo/title",
        "http://www.xvideos.com/video.demo/title",
        "https://user@www.xvideos.com/video.demo/title",
    ],
)
async def test_invalid_pages_are_rejected_before_network_access(url):
    def reject(request):
        pytest.fail("Invalid page must not reach the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        with pytest.raises(DownloadError):
            await XVideosDownloader(XVideosClient(http)).inspect(url)


async def test_router_refreshes_only_selected_source_and_uses_xvideos_headers():
    page_calls = 0
    visits = []

    def respond(request):
        nonlocal page_calls
        visits.append(str(request.url))
        if request.url.host == "www.xvideos.com":
            page_calls += 1
            assert request.headers["referer"] == "https://www.xvideos.com/"
            return httpx.Response(200, text=PAGE.replace("first", f"token{page_calls}"))
        assert request.headers["referer"] == PAGE_URL
        if request.url.path.endswith(".mp4"):
            assert request.headers["range"] == "bytes=0-1023"
            return httpx.Response(200, content=MP4)
        if request.url.path.endswith(".ts"):
            return httpx.Response(200, content=TS)
        return httpx.Response(200, text=PLAYLIST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        router = DownloaderRouter({"xvideos": XVideosDownloader(XVideosClient(http))})
        media = await router.inspect(PAGE_URL)
        visits.clear()
        source = await router.resolve(media, media.qualities[0])
    assert page_calls == 2 and "token2" in source.url
    assert source.protocol == "progressive" and source.headers["Referer"] == PAGE_URL
    assert len(visits) == 2 and not any("low.mp4" in url or ".m3u8" in url for url in visits)


async def test_hls_resolution_refresh_keeps_selected_video_and_separate_audio():
    page_calls = 0
    visits = []

    def respond(request):
        nonlocal page_calls
        visits.append(request.url.path)
        if request.url.host == "www.xvideos.com":
            page_calls += 1
            return httpx.Response(200, text=PAGE)
        if request.url.path.endswith(".mp4"):
            return httpx.Response(200, content=MP4)
        if request.url.path.endswith("master.m3u8"):
            return httpx.Response(
                200, text=MASTER.replace("1080.m3u8", f"1080.m3u8?t={page_calls}")
            )
        if request.url.path.endswith(".ts"):
            return httpx.Response(200, content=TS)
        return httpx.Response(200, text=PLAYLIST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XVideosDownloader(XVideosClient(http))
        media = await downloader.inspect(PAGE_URL)
        assert [item.key for item in media.qualities] == [
            "high",
            "hls_1920x1080_h264",
            "hls_1280x720_h264",
            "low",
        ]
        visits.clear()
        source = await downloader.resolve(media, media.qualities[1])
    assert source.url == "https://cdn.example/1080.m3u8?t=2"
    assert source.audio_url == "https://cdn.example/audio.m3u8"
    assert "720.m3u8" not in " ".join(visits) and not any(".mp4" in path for path in visits)


async def test_broken_sources_are_filtered_without_full_video_download():
    def respond(request):
        if request.url.host == "www.xvideos.com":
            return httpx.Response(200, text=PAGE)
        if request.url.path == "/low.mp4":
            return httpx.Response(200, content=MP4)
        if request.url.path == "/high.mp4":
            return httpx.Response(200, text="<html>error</html>")
        return httpx.Response(200, text=PLAYLIST.replace("120", "30"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await XVideosDownloader(XVideosClient(http)).inspect(PAGE_URL)
    assert [item.key for item in media.qualities] == ["low"]


async def test_disappeared_selected_quality_never_sends_a_lower_quality():
    calls = 0

    def respond(request):
        nonlocal calls
        if request.url.host == "www.xvideos.com":
            calls += 1
            page = PAGE if calls == 1 else "setVideoUrlLow('https://cdn.example/low.mp4')"
            return httpx.Response(200, text=page)
        return (
            httpx.Response(200, content=MP4)
            if request.url.path.endswith(".mp4")
            else (httpx.Response(200, text=PLAYLIST))
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XVideosDownloader(XVideosClient(http))
        media = await downloader.inspect(PAGE_URL)
        with pytest.raises(DownloadError, match="کیفیت انتخاب‌شده"):
            await downloader.resolve(media, media.qualities[0])


async def test_cross_site_selection_is_rejected_before_any_request():
    media = read_video(PAGE, PAGE_URL)

    def reject(request):
        pytest.fail("Invalid selection must not reach the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        downloader = XVideosDownloader(XVideosClient(http))
        with pytest.raises(DownloadError):
            await downloader.resolve(replace(media, site="xnxx"), media.qualities[0])
        with pytest.raises(DownloadError):
            await downloader.resolve(media, replace(media.qualities[0], key="unknown"))


async def test_redirect_outside_the_supported_site_is_rejected():
    visited = []

    def respond(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/video.demo"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError):
            await XVideosClient(http).page(PAGE_URL)
    assert visited == [PAGE_URL]


@pytest.mark.parametrize("status", [403, 404, 429, 500])
async def test_page_errors_preserve_status_and_hide_source_urls(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as http:
        with pytest.raises(SiteHTTPError) as failure:
            await XVideosClient(http).page(PAGE_URL)
    assert failure.value.status == status
    assert "XVideos" in str(failure.value) and PAGE_URL not in str(failure.value)


async def test_page_request_allows_other_bot_work_and_can_be_cancelled():
    started = asyncio.Event()

    async def respond(request):
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        task = asyncio.create_task(XVideosDownloader(XVideosClient(http)).inspect(PAGE_URL))
        await asyncio.wait_for(started.wait(), 1)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
