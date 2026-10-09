import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from downloader_bot.downloaders.router import DownloaderRouter
from downloader_bot.downloaders.xnxx.client import XNXXClient
from downloader_bot.downloaders.xnxx.downloader import XNXXDownloader
from downloader_bot.downloaders.xnxx.parser import (
    extract_links,
    hls_duration,
    player_string,
    read_video,
)
from downloader_bot.schemas.media import DownloadError, SiteHTTPError

PAGE_URL = "https://www.xnxx.com/video-demo/example"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"
PLAYLIST = "#EXTM3U\n#EXT-X-TARGETDURATION:120\n#EXTINF:120,\nsegment.ts\n#EXT-X-ENDLIST\n"
PAGE = r"""
<meta content="Demo &amp; Test" property="og:title">
<meta property="og:image" content="//cdn.example/thumbnail.jpg">
<title>Fallback title</title>
<script>
html5player.setVideoUrlHigh('https:\/\/cdn.example\/high.mp4?token=first\u0026a=1');
html5player.setVideoUrlLow("//cdn.example/low.mp4");
html5player.setVideoHLS('https://cdn.example/master.m3u8');
html5player.setVideoDuration(120.5);
</script>
"""


def test_supplied_extractor_handles_html_and_javascript_escaped_urls():
    media = read_video(PAGE, PAGE_URL)
    assert media.site == "xnxx" and media.content_id == "demo"
    assert media.title == "Demo & Test" and media.duration == 120
    assert media.thumbnail == "https://cdn.example/thumbnail.jpg"
    assert [quality.key for quality in media.qualities] == ["high", "hls", "low"]
    assert all(quality.mime_type == "video/mp4" for quality in media.qualities)
    assert media.qualities[0].endpoint == "https://cdn.example/high.mp4?token=first&a=1"
    assert media.qualities[2].endpoint == "https://cdn.example/low.mp4"


@pytest.mark.parametrize(
    ("html", "keys"),
    [
        ("setVideoUrlLow('https://cdn.example/low.mp4')", ["low"]),
        ("setVideoHLS('https://cdn.example/stream.m3u8')", ["hls"]),
    ],
)
def test_only_present_formats_are_offered(html, keys):
    media = read_video("<title>Demo / test?</title>" + html, PAGE_URL)
    assert media.title == "Demo test"
    assert [quality.key for quality in media.qualities] == keys


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///video.mp4", ""])
def test_non_media_links_are_not_offered(url):
    with pytest.raises(DownloadError):
        read_video(f"setVideoUrlHigh('{url}')", PAGE_URL)


def test_missing_title_defaults_to_video_and_non_video_page_is_rejected():
    assert extract_links("", PAGE_URL)["title"] == "video"
    with pytest.raises(DownloadError, match="صفحه یک ویدیو"):
        read_video(PAGE, "https://www.xnxx.com/")


async def test_selection_refreshes_signed_url_and_preserves_source_headers():
    calls = 0

    def respond(request):
        nonlocal calls
        if request.url.host == "cdn.example":
            return (
                httpx.Response(200, text=PLAYLIST)
                if request.url.path.endswith(".m3u8")
                else httpx.Response(200, content=MP4)
            )
        calls += 1
        assert request.headers["referer"] == "https://www.xnxx.com/"
        assert "Chrome/129" in request.headers["user-agent"]
        return httpx.Response(200, text=PAGE.replace("first", f"token{calls}"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XNXXDownloader(XNXXClient(http))
        media = await downloader.inspect(PAGE_URL)
        source = await downloader.resolve(media, media.qualities[0])
    assert calls == 2
    assert "token2" in source.url and "token1" not in source.url
    assert source.protocol == "progressive"
    assert source.headers["Referer"] == PAGE_URL


async def test_missing_selected_quality_is_reported_instead_of_sending_another_quality():
    calls = 0

    def respond(request):
        nonlocal calls
        if request.url.host == "cdn.example":
            return (
                httpx.Response(200, text=PLAYLIST)
                if request.url.path.endswith(".m3u8")
                else httpx.Response(200, content=MP4)
            )
        calls += 1
        page = PAGE if calls == 1 else "setVideoUrlLow('https://cdn.example/low.mp4')"
        return httpx.Response(200, text=page)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XNXXDownloader(XNXXClient(http))
        media = await downloader.inspect(PAGE_URL)
        with pytest.raises(DownloadError, match="کیفیت انتخاب‌شده"):
            await downloader.resolve(media, media.qualities[0])


async def test_foreign_redirect_is_rejected_before_request():
    visited = []

    def respond(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/video-demo"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError):
            await XNXXClient(http).page(PAGE_URL)
    assert visited == [PAGE_URL]


@pytest.mark.parametrize("status", [403, 404, 429, 500])
async def test_page_http_errors_keep_status_without_exposing_source_urls(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as http:
        with pytest.raises(SiteHTTPError) as failure:
            await XNXXClient(http).page(PAGE_URL)
    assert failure.value.status == status
    assert PAGE_URL not in str(failure.value)


async def test_page_request_yields_to_other_bot_work():
    started = asyncio.Event()
    release = asyncio.Event()

    async def respond(request):
        if request.url.host == "cdn.example":
            return (
                httpx.Response(200, text=PLAYLIST)
                if request.url.path.endswith(".m3u8")
                else httpx.Response(200, content=MP4)
            )
        started.set()
        await release.wait()
        return httpx.Response(200, text=PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        task = asyncio.create_task(XNXXDownloader(XNXXClient(http)).inspect(PAGE_URL))
        await asyncio.wait_for(started.wait(), 1)
        assert not task.done()
        release.set()
        assert (await task).content_id == "demo"


async def test_router_routes_inspection_and_selected_media_to_the_correct_site():
    calls = []

    def downloader(site):
        async def inspect(url):
            calls.append((site, "inspect", url))
            return read_video(PAGE, PAGE_URL)

        async def resolve(media, quality):
            calls.append((site, "resolve", quality.key))
            return "resolved"

        return SimpleNamespace(inspect=inspect, resolve=resolve)

    router = DownloaderRouter({site: downloader(site) for site in ("soundcloud", "xnxx")})
    media = await router.inspect(PAGE_URL)
    await router.inspect("https://soundcloud.com/artist/track")
    assert await router.resolve(media, media.qualities[0]) == "resolved"
    assert [call[:2] for call in calls] == [
        ("xnxx", "inspect"),
        ("soundcloud", "inspect"),
        ("xnxx", "resolve"),
    ]
    with pytest.raises(DownloadError):
        await router.inspect("https://example.com/video")


@pytest.mark.parametrize(
    "url",
    [
        "https://video.xnxx.com/video12345/title",
        "https://www.xnxx3.com/video-demo/title",
        "https://xnxx.com/video.demo/title",
    ],
)
def test_old_urls_and_aliases_have_stable_video_identity(url):
    assert read_video(PAGE, url).content_id in {"12345", "demo"}


def test_player_metadata_fallbacks_and_invalid_duration_do_not_crash():
    page = r"""
    <meta property="og:duration" content="nan">
    <title>Fallback</title>
    html5player.setVideoTitle('Demo \uD83D\uDE00 &amp; Test');
    html5player.setVideoDuration('12.5');
    html5player.setThumbUrl169('//cdn.example/wide.jpg');
    html5player.setVideoUrlHigh('https://cdn.example/high.mp4');
    """
    info = extract_links(page, PAGE_URL)
    assert info["title"] == "Demo 😀 & Test"
    assert info["duration"] == 12
    assert info["thumbnail"] == "https://cdn.example/wide.jpg"
    assert extract_links(page.replace("nan", "25"), PAGE_URL)["duration"] == 25


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example:bad/video.mp4",
        "https://user@cdn.example/video.mp4",
        r"https://cdn.example/\nvideo.mp4",
        r"\nhttps://cdn.example/video.mp4",
    ],
)
def test_invalid_or_control_characters_in_player_urls_are_rejected(url):
    with pytest.raises(DownloadError):
        read_video(f"setVideoUrlHigh('{url}')", PAGE_URL)


def test_placeholder_does_not_hide_the_later_real_player_url():
    info = extract_links(
        "setVideoUrlHigh(''); setVideoUrlHigh('https://cdn.example/video.mp4');",
        PAGE_URL,
    )
    assert info["high"] == "https://cdn.example/video.mp4"


@pytest.mark.parametrize("broken", ["html", "404", "timeout", "partial_hls"])
async def test_broken_sources_are_excluded_without_hiding_a_working_quality(broken):
    def respond(request):
        if request.url.host != "cdn.example":
            return httpx.Response(200, text=PAGE)
        if request.url.path == "/high.mp4":
            if broken == "timeout":
                raise httpx.ConnectTimeout("temporary CDN failure")
            return (
                httpx.Response(404)
                if broken == "404"
                else httpx.Response(200, text="<html>Error</html>")
            )
        if request.url.path.endswith(".m3u8"):
            return httpx.Response(
                200, text=PLAYLIST.replace("120", "30") if broken == "partial_hls" else PLAYLIST
            )
        return httpx.Response(200, content=MP4)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await XNXXDownloader(XNXXClient(http)).inspect(PAGE_URL)
    assert "high" not in [quality.key for quality in media.qualities]
    assert "low" in [quality.key for quality in media.qualities]
    assert ("hls" in [quality.key for quality in media.qualities]) == (broken != "partial_hls")


async def test_all_unavailable_formats_return_a_recoverable_error():
    def respond(request):
        return (
            httpx.Response(200, text=PAGE)
            if request.url.host != "cdn.example"
            else httpx.Response(404)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError, match="قابل دریافت"):
            await XNXXDownloader(XNXXClient(http)).inspect(PAGE_URL)


async def test_cdn_rate_limit_is_not_hidden_or_retried():
    visits = []

    def respond(request):
        visits.append(request.url.path)
        return (
            httpx.Response(200, text=PAGE)
            if request.url.host != "cdn.example"
            else httpx.Response(429)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(SiteHTTPError) as failure:
            await XNXXDownloader(XNXXClient(http)).inspect(PAGE_URL)
    assert failure.value.status == 429
    assert len(visits) == len(set(visits))


MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="Main",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="sound"
1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
360.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS="mp4a.40.2"
audio.m3u8
"""


async def test_hls_lists_playable_resolutions_and_resolves_selected_video_and_audio():
    page_calls = 0
    visits = []

    def respond(request):
        nonlocal page_calls
        visits.append(str(request.url))
        if request.url.host != "cdn.example":
            page_calls += 1
            return httpx.Response(200, text=PAGE)
        assert request.headers["referer"] == PAGE_URL
        if request.url.path.endswith(".mp4"):
            assert request.headers["range"] == "bytes=0-1023"
            return httpx.Response(200, content=MP4)
        if request.url.path.endswith("/master.m3u8"):
            master = MASTER.replace("1080.m3u8", f"1080.m3u8?token={page_calls}")
            return httpx.Response(200, text=master)
        if request.url.path.endswith("360.m3u8"):
            return httpx.Response(404)
        return httpx.Response(200, text=PLAYLIST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XNXXDownloader(XNXXClient(http))
        media = await downloader.inspect(PAGE_URL)
        assert [item.key for item in media.qualities] == [
            "high",
            "hls_1920x1080_h264",
            "hls_1280x720_h264",
            "low",
        ]
        visits.clear()
        source = await downloader.resolve(media, media.qualities[1])
    assert source.url == "https://cdn.example/1080.m3u8?token=2"
    assert source.audio_url == "https://cdn.example/audio.m3u8"
    assert source.require_audio
    assert source.duration == 120
    assert media.qualities[1].duration == 120
    assert not any(".mp4" in url or "720.m3u8" in url or "360.m3u8" in url for url in visits)


async def test_changed_video_identity_is_rejected_before_probing_new_sources():
    visits = []

    def respond(request):
        visits.append(str(request.url))
        if len(visits) == 1:
            return httpx.Response(302, headers={"location": "/video-other/title"})
        return httpx.Response(200, text=PAGE)

    media = read_video(PAGE, PAGE_URL)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError, match="تغییر"):
            await XNXXDownloader(XNXXClient(http)).resolve(media, media.qualities[0])
    assert len(visits) == 2


async def test_duplicate_high_and_low_url_is_offered_only_once():
    def respond(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=MP4)
        return httpx.Response(
            200,
            text="setVideoUrlHigh('https://cdn.example/same.mp4'); setVideoUrlLow('https://cdn.example/same.mp4');",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await XNXXDownloader(XNXXClient(http)).inspect(PAGE_URL)
    assert [item.key for item in media.qualities] == ["high"]


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text.replace("#EXT-X-ENDLIST", ""),
        lambda text: text.replace("segment.ts", "file:///secret.ts"),
        lambda text: text.replace("#EXTINF:120,", "#EXTINF:nan,"),
        lambda text: text.replace("#EXTINF:120,", "#EXT-X-GAP\n#EXTINF:120,"),
        lambda text: text.replace(
            "#EXTINF:120,", '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"\n#EXTINF:120,'
        ),
    ],
)
def test_live_invalid_missing_and_unsupported_hls_is_rejected(change):
    with pytest.raises(DownloadError):
        hls_duration(change(PLAYLIST), "https://cdn.example/playlist.m3u8", 120)


async def test_resolve_does_not_accept_a_quality_from_another_media():
    def reject(request):
        pytest.fail("Invalid selections must be rejected before requesting the site")

    media = read_video(PAGE, PAGE_URL)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        downloader = XNXXDownloader(XNXXClient(http))
        with pytest.raises(DownloadError):
            await downloader.resolve(replace(media, site="soundcloud"), media.qualities[0])
        with pytest.raises(DownloadError):
            await downloader.resolve(media, replace(media.qualities[0], key="unknown"))


async def test_mp4_probe_closes_an_ignored_range_response_after_a_small_sample():
    class Body(httpx.AsyncByteStream):
        consumed = 0
        closed = False

        async def __aiter__(self):
            for _ in range(1000):
                self.consumed += 1
                yield MP4 + b"x" * (1024 - len(MP4))

        async def aclose(self):
            self.closed = True

    body = Body()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
    ) as http:
        assert await XNXXClient(http).is_mp4("https://cdn.example/video.mp4", PAGE_URL)
    assert body.consumed == 1 and body.closed


async def test_manifest_resolution_is_bounded_and_highest_variants_come_first():
    active = peak = 0
    master = "#EXTM3U\n" + "".join(
        f'#EXT-X-STREAM-INF:BANDWIDTH={height * 1000},RESOLUTION=1280x{height},CODECS="avc1.4d401f"\n{height}.m3u8\n'
        for height in range(700, 708)
    )

    async def respond(request):
        nonlocal active, peak
        if request.url.host != "cdn.example":
            return httpx.Response(200, text="setVideoHLS('https://cdn.example/master.m3u8');")
        if request.url.path.endswith("master.m3u8"):
            return httpx.Response(200, text=master)
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, text=PLAYLIST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await XNXXDownloader(XNXXClient(http)).inspect(PAGE_URL)
    assert peak == 4
    assert [item.height for item in media.qualities] == list(reversed(range(700, 708)))
    assert media.duration == 120


@pytest.mark.parametrize("audio_failure", ["403", "short", "long"])
async def test_unavailable_or_mismatched_external_audio_does_not_offer_a_silent_video(
    audio_failure,
):
    def respond(request):
        if request.url.host != "cdn.example":
            return httpx.Response(200, text="setVideoHLS('https://cdn.example/master.m3u8');")
        if request.url.path.endswith("master.m3u8"):
            return httpx.Response(200, text=MASTER)
        if request.url.path.endswith("audio.m3u8"):
            if audio_failure == "403":
                return httpx.Response(403)
            return httpx.Response(
                200, text=PLAYLIST.replace("120", "30" if audio_failure == "short" else "300")
            )
        return httpx.Response(200, text=PLAYLIST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await XNXXDownloader(XNXXClient(http)).inspect(PAGE_URL)
    assert "hls_1920x1080_h264" not in [item.key for item in media.qualities]
    assert "hls_1280x720_h264" in [item.key for item in media.qualities]


async def test_a_disappeared_selected_hls_variant_never_falls_back_to_lower_resolution():
    calls = 0

    def respond(request):
        nonlocal calls
        if request.url.host != "cdn.example":
            calls += 1
            return httpx.Response(200, text="setVideoHLS('https://cdn.example/master.m3u8');")
        if request.url.path.endswith("master.m3u8"):
            master = (
                MASTER
                if calls == 1
                else ("#EXTM3U\n" + MASTER[MASTER.index("#EXT-X-STREAM-INF:BANDWIDTH=1500000") :])
            )
            return httpx.Response(200, text=master)
        return httpx.Response(200, text=PLAYLIST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XNXXDownloader(XNXXClient(http))
        media = await downloader.inspect(PAGE_URL)
        with pytest.raises(DownloadError, match="کیفیت انتخاب‌شده"):
            await downloader.resolve(media, media.qualities[0])


async def test_video_lists_are_rejected_before_network_access():
    def reject(request):
        pytest.fail("Video lists must not reach the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        with pytest.raises(DownloadError):
            await XNXXDownloader(XNXXClient(http)).inspect("https://www.xnxx.com/search/demo")


async def test_oversized_page_response_is_closed_before_reading_the_whole_body():
    class Body(httpx.AsyncByteStream):
        consumed = 0
        closed = False

        async def __aiter__(self):
            for _ in range(1000):
                self.consumed += 1
                yield b"x" * (64 * 1024)

        async def aclose(self):
            self.closed = True

    body = Body()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
    ) as http:
        with pytest.raises(DownloadError, match="حجم پاسخ"):
            await XNXXClient(http).page(PAGE_URL)
    assert body.consumed == 65 and body.closed


async def test_mp4_with_a_leading_padding_box_is_not_rejected():
    data = b"\x00\x00\x00\x08free" + MP4
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data))
    ) as http:
        assert await XNXXClient(http).is_mp4("https://cdn.example/video.mp4", PAGE_URL)


async def test_progressive_selection_keeps_final_cdn_url_and_complete_size_after_refresh():
    page_calls = 0
    cdn_calls = []

    def respond(request):
        nonlocal page_calls
        if request.url.host == "www.xnxx.com":
            page_calls += 1
            return httpx.Response(
                200, text=(f"setVideoUrlHigh('https://cdn.example/entry-{page_calls}.mp4');")
            )
        cdn_calls.append(str(request.url))
        assert request.headers["range"] == "bytes=0-1023"
        assert request.headers["accept-encoding"] == "identity"
        if request.url.host == "cdn.example":
            return httpx.Response(
                302,
                headers={
                    "location": f"https://edge.example/{request.url.path.lstrip('/')}?fresh=1",
                },
            )
        return httpx.Response(
            206,
            content=MP4 + b"x" * (1024 - len(MP4)),
            headers={
                "content-range": "bytes 0-1023/4329304",
                "content-type": "video/mp4",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XNXXDownloader(XNXXClient(http))
        media = await downloader.inspect(PAGE_URL)
        source = await downloader.resolve(media, media.qualities[0])
    assert media.qualities[0].endpoint == "https://edge.example/entry-1.mp4?fresh=1"
    assert source.url == "https://edge.example/entry-2.mp4?fresh=1"
    assert source.size_bytes == 4329304
    assert source.headers["Referer"] == PAGE_URL
    assert len(cdn_calls) == 4 and page_calls == 2


async def test_progressive_aliases_redirecting_to_one_file_offer_only_one_quality():
    def respond(request):
        if request.url.host == "www.xnxx.com":
            return httpx.Response(
                200,
                text=(
                    "setVideoUrlHigh('https://cdn.example/high.mp4');"
                    "setVideoUrlLow('https://cdn.example/low.mp4');"
                ),
            )
        if request.url.host == "cdn.example":
            return httpx.Response(302, headers={"location": "https://edge.example/same.mp4"})
        return httpx.Response(200, content=MP4)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await XNXXDownloader(XNXXClient(http)).inspect(PAGE_URL)
    assert len(media.qualities) == 1 and media.qualities[0].key == "high"


@pytest.mark.parametrize(
    "content_range",
    [
        "",
        "bytes 1-24/100",
        "bytes 0-23/*",
        "bytes 0-23/23",
        "bytes 0-100/200",
    ],
)
async def test_mp4_probe_rejects_invalid_or_incomplete_sample_ranges(content_range):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                206,
                content=MP4,
                headers={
                    "content-range": content_range,
                },
            )
        )
    ) as http:
        assert await XNXXClient(http).mp4("https://cdn.example/video.mp4", PAGE_URL) is None


async def test_mp4_probe_accepts_a_small_complete_ranged_file_without_using_partial_length():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                206,
                content=MP4,
                headers={
                    "content-range": f"bytes 0-{len(MP4) - 1}/{len(MP4)}",
                },
            )
        )
    ) as http:
        probe = await XNXXClient(http).mp4("https://cdn.example/video.mp4", PAGE_URL)
    assert probe.size_bytes == len(MP4)


@pytest.mark.parametrize("suffix", ["", "/", "?keep=1", "/?keep=1"])
async def test_video_links_without_a_title_segment_get_a_usable_page_url(suffix):
    def respond(request):
        assert request.url.path == "/video-demo/video"
        assert request.url.query == (b"keep=1" if "?" in suffix else b"")
        return httpx.Response(200, text=PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        page, final_url = await XNXXClient(http).page("https://www.xnxx.com/video-demo" + suffix)
    assert read_video(page, final_url).content_id == "demo"


async def test_malformed_escaped_player_string_finishes_without_exponential_backtracking():
    code = r"""
import sys
sys.path.insert(0, sys.argv[1])
from downloader_bot.downloaders.xnxx.parser import player_url
malformed = "setVideoUrlHigh('" + "\\" * 2048 + "broken"
valid = "\nsetVideoUrlHigh('https://cdn.example/video.mp4')"
assert player_url(malformed + valid, "setVideoUrlHigh", "https://www.xnxx.com/video-demo") == "https://cdn.example/video.mp4"
print("parsed")
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(Path(__file__).parents[1] / "src"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert stdout.strip() == b"parsed"
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


@pytest.mark.parametrize(
    "literal,expected",
    [
        (r"'Demo \' title'", "Demo ' title"),
        (r'"Demo \" title"', 'Demo " title'),
    ],
)
def test_escaped_quotes_do_not_end_the_player_value(literal, expected):
    page = f"setVideoTitle({literal}); setVideoUrlHigh('https://cdn.example/video.mp4')"
    assert player_string(page, "setVideoTitle") == expected
