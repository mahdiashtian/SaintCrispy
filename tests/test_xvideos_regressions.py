import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from downloader_bot.downloaders.xvideos.client import XVideosClient
from downloader_bot.downloaders.xvideos.downloader import XVideosDownloader
from downloader_bot.downloaders.xvideos.parser import (
    extract_links,
    hls_duration,
    player_string,
    read_hls_variants,
    read_video,
)
from downloader_bot.downloaders.xvideos.urls import extract_url
from downloader_bot.models import DownloadError, SiteHTTPError
from downloader_bot.streaming import media_chunks

PAGE_URL = "https://www.xvideos.com/video.demo/example"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"
TS = (b"\x47" + b"\x00" * 187) * 6
PLAYLIST = "#EXTM3U\n#EXTINF:120,\nfirst.ts\n#EXT-X-ENDLIST\n"
PAGE = "setVideoHLS('https://cdn.example/master.m3u8'); setVideoDuration(120);"


@pytest.mark.parametrize(
    "url",
    [
        "https://fr.xvideos.com/video.demo/title",
        "https://de.xvideos2.com/video.demo/title",
        "https://www.xvideos.es/video.demo/title",
        "https://flashservice.xvideos.com/embedframe/demo",
    ],
)
def test_official_aliases_and_embeds_identify_the_same_video(url):
    assert extract_url(url) == url
    assert read_video("setVideoUrlHigh('https://cdn.example/video.mp4')", url).content_id == "demo"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.xvideos.com/videolist",
        "https://evil.xvideos2.com/video.demo/title",
        "https://xvideos.es.evil.org/video.demo/title",
        "https://www.xvideos.com/video.demo\\evil",
    ],
)
async def test_lists_and_invalid_hosts_never_reach_the_network(url):
    def reject(request):
        pytest.fail("Invalid URLs must fail before network access")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        with pytest.raises(DownloadError):
            await XVideosDownloader(XVideosClient(http)).inspect(url)


def test_structured_video_source_takes_priority_over_unrelated_literal_links():
    info = extract_links(
        '<script>const ad="https://cdn.example/ad.mp4";</script>'
        '<video><source src="https://cdn.example/actual.mp4"></video>',
        PAGE_URL,
    )
    assert info["high"] == "https://cdn.example/actual.mp4"


def test_invalid_later_player_call_does_not_hide_an_earlier_valid_url():
    info = extract_links(
        "setVideoUrlHigh('https://cdn.example/video.mp4'); setVideoUrlHigh('file:///bad.mp4');",
        PAGE_URL,
        fallback_links=False,
    )
    assert info["high"] == "https://cdn.example/video.mp4"


async def test_malformed_player_string_does_not_block_the_event_loop():
    code = r"""
import sys
sys.path.insert(0, sys.argv[1])
from downloader_bot.downloaders.xvideos.parser import player_url
page = "setVideoUrlHigh('" + "\\" * 2048 + "broken"
page += "\nsetVideoUrlHigh('https://cdn.example/video.mp4')"
assert player_url(page, "setVideoUrlHigh", "https://www.xvideos.com/video.demo/_") == "https://cdn.example/video.mp4"
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
        _, diagnostic = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, diagnostic.decode(errors="replace")
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
def test_escaped_quote_is_part_of_the_player_string(literal, expected):
    assert player_string(f"setVideoTitle({literal})", "setVideoTitle") == expected


def test_duration_span_and_wide_thumbnail_fallbacks():
    info = extract_links(
        '<span class="duration">20:38</span>'
        "setThumbUrl('https://cdn.example/small.jpg');"
        "setThumbUrl169('https://cdn.example/wide.jpg');",
        PAGE_URL,
    )
    assert info["duration"] == 1238
    assert info["thumbnail"] == "https://cdn.example/wide.jpg"


def test_jsonld_video_metadata_and_signed_content_url_are_used():
    page = """<script type="application/ld+json">{"@type":"VideoObject",
      "name":"Demo", "duration":"PT1M2.5S", "thumbnailUrl":["https://cdn.example/thumb.jpg"],
      "contentUrl":"https://cdn.example/video.mp4?token=one&two=2"}</script>"""
    info = extract_links(page, PAGE_URL)
    assert info["title"] == "Demo" and info["duration"] == 62
    assert info["thumbnail"] == "https://cdn.example/thumb.jpg"
    assert info["high"] == "https://cdn.example/video.mp4?token=one&two=2"


def test_http_success_error_page_cannot_offer_unrelated_media():
    page = '<h1 class="inlineError">Unavailable</h1><source src="https://cdn.example/ad.mp4">'
    with pytest.raises(DownloadError):
        read_video(page, PAGE_URL)


@pytest.mark.parametrize(
    "playlist",
    [
        "#EXTM3U\n#EXTINF:60,\n#EXTINF:120,\nfirst.ts\n#EXT-X-ENDLIST\n",
        "#EXTM3U\n#EXTINF:120,\n#EXT-X-BYTERANGE:10\nfirst.ts\n#EXT-X-ENDLIST\n",
        '#EXTM3U\n#EXT-X-MAP:BYTERANGE="10@0"\n#EXTINF:120,\nfirst.ts\n#EXT-X-ENDLIST\n',
    ],
)
def test_malformed_hls_is_rejected_instead_of_silently_skipping_data(playlist):
    with pytest.raises(DownloadError):
        hls_duration(playlist, "https://cdn.example/master.m3u8", 120)


@pytest.mark.parametrize(
    "resolution",
    [
        "9" * 5000 + "x720",
        "1280x" + "9" * 5000,
        "0x720",
        "1280x0",
        "12xBAD",
    ],
)
def test_invalid_hls_resolution_cannot_crash_or_offer_a_misidentified_quality(resolution):
    master = (
        '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,CODECS="avc1",RESOLUTION='
        + resolution
        + "\nvideo.m3u8\n"
    )
    assert read_hls_variants(master, "https://cdn.example/master.m3u8") == ()


async def test_a_working_alternative_of_the_same_hls_quality_is_preserved():
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
broken.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
working.m3u8
"""

    def respond(request):
        if request.url.host == "www.xvideos.com":
            return httpx.Response(200, text=PAGE)
        if request.url.path.endswith("master.m3u8"):
            return httpx.Response(200, text=master)
        if request.url.path.endswith("broken.m3u8"):
            return httpx.Response(404)
        return (
            httpx.Response(200, content=TS)
            if request.url.path.endswith(".ts")
            else (httpx.Response(200, text=PLAYLIST))
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XVideosDownloader(XVideosClient(http))
        media = await downloader.inspect(PAGE_URL)
        assert len(media.qualities) == 1 and media.qualities[0].key == "hls_1280x720_h264"
        source = await downloader.resolve(media, media.qualities[0])
    assert source.url == "https://cdn.example/working.m3u8"
    assert source.duration == 120 and source.require_audio


@pytest.mark.parametrize("failure", ["404", "html"])
async def test_manifest_with_unavailable_or_html_segments_is_not_offered(failure):
    def respond(request):
        if request.url.host == "www.xvideos.com":
            return httpx.Response(200, text=PAGE)
        if request.url.path.endswith(".ts"):
            return (
                httpx.Response(404)
                if failure == "404"
                else httpx.Response(200, text="<html>error</html>")
            )
        return httpx.Response(200, text=PLAYLIST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError, match="قابل دریافت"):
            await XVideosDownloader(XVideosClient(http)).inspect(PAGE_URL)


async def test_progressive_resolution_preserves_cdn_redirect_and_total_size():
    def respond(request):
        if request.url.host == "www.xvideos.com":
            return httpx.Response(200, text="setVideoUrlHigh('https://cdn.example/original.mp4');")
        if request.url.path == "/original.mp4":
            return httpx.Response(302, headers={"location": "/fresh.mp4?token=new"})
        return httpx.Response(200, content=MP4)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XVideosDownloader(XVideosClient(http))
        media = await downloader.inspect(PAGE_URL)
        source = await downloader.resolve(media, media.qualities[0])
    assert source.url == "https://cdn.example/fresh.mp4?token=new"
    assert source.size_bytes == len(MP4)


async def test_temporary_page_failure_gets_one_bounded_retry():
    calls = 0

    def respond(request):
        nonlocal calls
        if request.url.host == "www.xvideos.com":
            calls += 1
            return (
                httpx.Response(503)
                if calls == 1
                else (httpx.Response(200, text="setVideoUrlHigh('https://cdn.example/video.mp4');"))
            )
        return httpx.Response(200, content=MP4)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await XVideosDownloader(XVideosClient(http)).inspect(PAGE_URL)
    assert media.content_id == "demo" and calls == 2


async def test_rate_limit_cancels_other_checks_and_prevents_immediate_retry():
    visits = []
    slow_closed = asyncio.Event()

    async def respond(request):
        visits.append(request.url.path)
        if request.url.host == "www.xvideos.com":
            return httpx.Response(
                200,
                text=(
                    "setVideoUrlHigh('https://cdn.example/high.mp4');"
                    "setVideoUrlLow('https://cdn.example/low.mp4');"
                ),
            )
        if request.url.path == "/high.mp4":
            await asyncio.sleep(0.01)
            return httpx.Response(429, headers={"Retry-After": "30"})
        try:
            await asyncio.Event().wait()
        finally:
            slow_closed.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XVideosDownloader(XVideosClient(http))
        with pytest.raises(SiteHTTPError) as failure:
            await asyncio.wait_for(downloader.inspect(PAGE_URL), 1)
        assert failure.value.status == 429 and slow_closed.is_set()
        first_visits = list(visits)
        with pytest.raises(SiteHTTPError):
            await downloader.inspect(PAGE_URL)
    assert visits == first_visits


async def test_queued_request_observes_a_rate_limit_from_an_active_request():
    entered = asyncio.Event()
    release_limited = asyncio.Event()
    release_others = asyncio.Event()
    visits = []

    async def respond(request):
        visits.append(request.url.path)
        if len(visits) == 4:
            entered.set()
        if request.url.path.endswith("limited"):
            await release_limited.wait()
            return httpx.Response(429, headers={"Retry-After": "30"})
        await release_others.wait()
        return httpx.Response(200, text="page")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = XVideosClient(http)
        tasks = [
            asyncio.create_task(client.page(PAGE_URL + name))
            for name in (
                "limited",
                "one",
                "two",
                "three",
            )
        ]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            queued = asyncio.create_task(client.page(PAGE_URL + "queued"))
            tasks.append(queued)
            await asyncio.sleep(0)
            release_limited.set()
            with pytest.raises(SiteHTTPError):
                await tasks[0]
            release_others.set()
            with pytest.raises(SiteHTTPError):
                await queued
            assert len(visits) == 4
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("fragment", ["quickies/a/demo", "quickies/a/12345"])
async def test_quickies_are_normalized_before_loading_the_page(fragment):
    visits = []

    def respond(request):
        visits.append(str(request.url))
        if request.url.host == "www.xvideos.com":
            return httpx.Response(200, text="setVideoUrlHigh('https://cdn.example/video.mp4');")
        return httpx.Response(200, content=MP4)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await XVideosDownloader(XVideosClient(http)).inspect(
            "https://www.xvideos.com/profiles/example#" + fragment,
        )
    identity = fragment.rsplit("/", 1)[1]
    assert media.content_id == identity
    assert (
        visits[0]
        == f"https://www.xvideos.com/video{'' if identity.isdecimal() else '.'}{identity}/_"
    )


@pytest.mark.parametrize(
    "content_range", [None, "bytes 1-24/100", "bytes 0-1024/2000", "bytes 0-24/24"]
)
async def test_invalid_partial_mp4_responses_are_rejected(content_range):
    headers = {"content-range": content_range} if content_range else {}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(206, headers=headers, content=MP4)
        )
    ) as http:
        with pytest.raises(DownloadError, match="محدوده"):
            await XVideosClient(http).mp4_source("https://cdn.example/video.mp4", PAGE_URL)


async def test_ignored_range_is_closed_after_one_small_sample():
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
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=body, headers={"Content-Length": str(1024 * 1000)}
            )
        )
    ) as http:
        source = await XVideosClient(http).mp4_source("https://cdn.example/video.mp4", PAGE_URL)
    assert source.size_bytes == 1024 * 1000
    assert body.consumed == 1 and body.closed


async def test_hls_byte_ranges_probe_both_ends_at_the_correct_offsets():
    ranges = []
    data = TS[:376] * 2
    playlist = (
        "#EXTM3U\n#EXTINF:60,\n#EXT-X-BYTERANGE:376@0\nsegments.ts\n"
        "#EXTINF:60,\n#EXT-X-BYTERANGE:376\nsegments.ts\n#EXT-X-ENDLIST\n"
    )

    def respond(request):
        if request.url.host == "www.xvideos.com":
            return httpx.Response(200, text=PAGE)
        if request.url.path.endswith(".ts"):
            value = request.headers["range"]
            ranges.append(value)
            start, end = map(int, value.removeprefix("bytes=").split("-"))
            return httpx.Response(
                206,
                content=data[start : end + 1],
                headers={
                    "Content-Range": f"bytes {start}-{end}/{len(data)}",
                },
            )
        return httpx.Response(200, text=playlist)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await XVideosDownloader(XVideosClient(http)).inspect(PAGE_URL)
    assert media.qualities[0].key == "hls" and ranges == ["bytes=0-375", "bytes=376-751"]


@pytest.mark.parametrize("key", [b"k" * 16, b"k" * 32])
async def test_aes_hls_requires_a_valid_key_sample(key):
    playlist = (
        '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:120,\nfirst.ts\n#EXT-X-ENDLIST\n'
    )

    def respond(request):
        if request.url.host == "www.xvideos.com":
            return httpx.Response(200, text=PAGE)
        if request.url.path.endswith("key.bin"):
            return httpx.Response(200, content=key)
        if request.url.path.endswith(".ts"):
            return httpx.Response(200, content=b"x" * 1024)
        return httpx.Response(200, text=playlist)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XVideosDownloader(XVideosClient(http))
        if len(key) == 16:
            assert (await downloader.inspect(PAGE_URL)).qualities[0].key == "hls"
        else:
            with pytest.raises(DownloadError, match="قابل دریافت"):
                await downloader.inspect(PAGE_URL)


@pytest.mark.parametrize("audio", [True, False])
async def test_resolved_hls_preserves_audio_and_detects_incomplete_ffmpeg_output(tmp_path, audio):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    manifest = tmp_path / "video.m3u8"
    create = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=32x32:r=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        *(["-c:a", "aac"] if audio else ["-an"]),
        "-f",
        "hls",
        str(manifest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await create.communicate()
    assert create.returncode == 0, diagnostic.decode(errors="replace")
    master = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=32x32,CODECS="avc1.42e01e,mp4a.40.2"\nvideo.m3u8\n'

    def respond(request):
        if request.url.host == "www.xvideos.com":
            return httpx.Response(
                200, text="setVideoHLS('https://cdn.example/master.m3u8'); setVideoDuration(1);"
            )
        if request.url.path.endswith("master.m3u8"):
            return httpx.Response(200, text=master)
        data = (tmp_path / request.url.path.rsplit("/", 1)[1]).read_bytes()
        if "range" in request.headers:
            start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
            end = min(end, len(data) - 1)
            return httpx.Response(
                206,
                content=data[start : end + 1],
                headers={
                    "Content-Range": f"bytes {start}-{end}/{len(data)}",
                },
            )
        return httpx.Response(200, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XVideosDownloader(XVideosClient(http))
        media = await downloader.inspect(PAGE_URL)
        quality = media.qualities[0]
        source = await downloader.resolve(media, quality)
        assert source.require_audio and source.duration == 1
        local = replace(source, url=str(manifest), headers={})
        if not audio:
            with pytest.raises(DownloadError):
                _ = [chunk async for chunk in media_chunks(http, local, quality, ffmpeg)]
            return
        output = b"".join([chunk async for chunk in media_chunks(http, local, quality, ffmpeg)])
        verify = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, diagnostic = await verify.communicate(output)
        assert verify.returncode == 0, diagnostic.decode(errors="replace")
        with pytest.raises(DownloadError, match="کامل نشد"):
            _ = [
                chunk
                async for chunk in media_chunks(http, replace(local, duration=10), quality, ffmpeg)
            ]
