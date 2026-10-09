import asyncio
import copy
import sys
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from downloader_bot.downloaders.router import DownloaderRouter
from downloader_bot.downloaders.youtube.client import YouTubeClient, extraction_error
from downloader_bot.downloaders.youtube.downloader import YouTubeDownloader
from downloader_bot.downloaders.youtube.handler import handle_youtube_link
from downloader_bot.downloaders.youtube.parser import (
    read_media,
    selections,
    source_headers,
    video_id,
)
from downloader_bot.handlers import register_handlers
from downloader_bot.menus import MenuStore
from downloader_bot.models import DownloadError, Source
from downloader_bot.progress import TransferProgress
from downloader_bot.streaming import media_chunks, remux_arguments
from downloader_bot.urls import extract_youtube_url

IDENTITY = "jNQXAC9IVRw"
PAGE = f"https://www.youtube.com/watch?v={IDENTITY}"


async def all_available(items):
    return {item["url"] for item in items}


def fmt(identity, height=None, video="none", audio="none", **extra):
    return {
        "format_id": identity,
        "url": f"https://cdn.example/{identity}?expires=old",
        "protocol": "https",
        "ext": "mp4",
        "height": height,
        "width": height * 16 // 9 if height else None,
        "vcodec": video,
        "acodec": audio,
        "fps": 30,
        "http_headers": {"User-Agent": identity},
        **extra,
    }


def info():
    return {
        "id": IDENTITY,
        "title": "Demo",
        "uploader": "Author",
        "duration": 19.3,
        "formats": [
            fmt("18", 360, "avc1", "mp4a.40.2", tbr=250),
            fmt("137", 1080, "avc1", tbr=3000),
            fmt("299", 1080, "avc1", fps=60, tbr=4000),
            fmt("401", 2160, "av01", tbr=5000),
            fmt("313", 2160, "vp9", tbr=6000),
            fmt("140", audio="mp4a.40.2", abr=128, language="en", language_preference=10),
            fmt("140-dub", audio="mp4a.40.2", abr=192, language="fr", language_preference=-10),
            fmt("251", audio="opus", abr=160),
            fmt("drm", 4320, "av01", has_drm=True),
            fmt("sabr", 4320, "av01", url=None),
            fmt("storyboard", video="images", protocol="mhtml"),
        ],
    }


async def test_youtube_menu_and_callback_registration():
    responses, registrations = [], []
    media = read_media(info(), IDENTITY)

    async def inspect(url):
        return media

    async def respond(text, **kwargs):
        responses.append(kwargs)

    menus = MenuStore()
    await handle_youtube_link(
        SimpleNamespace(
            raw_text=PAGE,
            sender_id=1,
            chat_id=2,
            respond=respond,
        ),
        SimpleNamespace(inspect=inspect),
        menus,
    )
    prefix, token, _ = responses[0]["buttons"][0][0].type.data.decode().split(":")
    assert prefix == "yt" and menus.get(token, 1, 2).media.site == "youtube"
    register_handlers(
        SimpleNamespace(
            add_event_handler=lambda handler, event: registrations.append((handler, event)),
        ),
        None,
        None,
    )
    handlers = {
        getattr(handler, "func", handler).__name__: event for handler, event in registrations
    }
    assert handlers["handle_youtube_link"].pattern(PAGE)
    assert handlers["handle_quality"].match(f"yt:{token}:0".encode())


@pytest.mark.parametrize(
    "url",
    [
        PAGE,
        f"https://youtu.be/{IDENTITY}?si=abc",
        f"https://m.youtube.com/watch?v={IDENTITY}&list=PLx",
        f"https://youtube.com/shorts/{IDENTITY}",
        f"https://youtube.com/live/{IDENTITY}",
        f"https://www.youtube-nocookie.com/embed/{IDENTITY}",
        f"https://music.youtube.com/watch?v={IDENTITY}",
    ],
)
def test_video_aliases_have_the_same_identity(url):
    assert video_id(url) == IDENTITY
    assert video_id(extract_youtube_url(f"لینک ({url}).")) == IDENTITY


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/",
        "https://youtube.com/playlist?list=x",
        "https://youtube.com/@channel",
        "https://youtube.com/watch?v=bad",
        PAGE + "&v=BaW_jenozKc",
        f"https://youtu.be/{IDENTITY}/extra",
        PAGE.replace("youtube.com", "youtube.com.evil.org"),
        PAGE.replace("youtube.com", "youtube.com@evil.org"),
        PAGE.replace("youtube.com", "youtube.com:443"),
        PAGE.replace("https:", "file:"),
    ],
)
def test_nonvideo_and_lookalike_links_are_rejected(url):
    with pytest.raises(DownloadError):
        video_id(url)


def test_only_complete_formats_are_offered_and_high_quality_has_audio():
    media = read_media(info(), IDENTITY)
    assert media.duration == 20
    assert len(media.qualities) == 7
    choices = selections(info())
    assert not any(item.quality.height == 4320 for item in choices)
    high = next(item for item in choices if item.quality.key == "video_1080p30_h264_mp4")
    assert high.audio["format_id"] == "140"  # Original audio, not the higher-bitrate dub.
    source = high.source("")
    assert source.protocol == "dash" and source.audio_url
    assert source.headers["User-Agent"] == "137"
    assert source.audio_headers["User-Agent"] == "140"
    low = next(item for item in choices if item.quality.height == 360)
    assert low.source("").protocol == "progressive" and low.audio is None
    assert next(item for item in choices if item.quality.codec == "vp9").quality.extension == "webm"


def test_silent_video_without_a_compatible_audio_is_not_offered():
    data = info()
    data["formats"] = [fmt("137", 1080, "avc1")]
    with pytest.raises(DownloadError):
        read_media(data, IDENTITY)


@pytest.mark.parametrize("video_hls", [True, False])
def test_mixed_hls_and_progressive_inputs_apply_hls_options_to_the_correct_track(video_hls):
    data = info()
    video = fmt("137", 1080, "avc1", protocol="m3u8_native" if video_hls else "https")
    audio = fmt("140", audio="mp4a.40.2", protocol="https" if video_hls else "m3u8_native")
    data["formats"] = [video, audio]
    choice = next(item for item in selections(data) if item.quality.mime_type.startswith("video/"))
    source = choice.source("")
    assert source.protocol == "hls"
    assert source.input_protocol == ("hls" if video_hls else "progressive")
    assert source.audio_protocol == ("progressive" if video_hls else "hls")
    arguments = remux_arguments(source, choice.quality)
    assert arguments.count("-http_seekable") == 1
    first_input = arguments.index("-i")
    second_input = arguments.index("-i", first_input + 1)
    option = arguments.index("-http_seekable")
    assert (option < first_input) if video_hls else (first_input < option < second_input)


def test_every_native_original_audio_quality_is_offered_separately():
    data = info()
    data["formats"].extend(
        [
            fmt("139", audio="mp4a.40.2", abr=48, language="en", language_preference=10),
            fmt("249", audio="opus", abr=50),
            fmt("250", audio="opus", abr=70),
            fmt(
                "258",
                audio="mp4a.40.2",
                abr=384,
                language="en",
                language_preference=10,
                audio_channels=6,
            ),
        ]
    )
    choices = selections(data)
    audio = [item for item in choices if item.quality.mime_type.startswith("audio/")]
    assert {item.video["format_id"] for item in audio} == {"139", "140", "258", "249", "250", "251"}
    assert [item.quality.bitrate for item in audio] == [384, 128, 48, 160, 70, 50]
    assert all(item.audio is None and item.source("").audio_url is None for item in audio)
    assert "6ch" in audio[0].quality.label
    assert next(item for item in choices if item.quality.height == 1080).audio["language"] == "en"


def test_hdr_and_sdr_versions_at_the_same_resolution_are_both_offered():
    data = info()
    data["formats"].append(fmt("337", 2160, "vp9", dynamic_range="HDR10", tbr=6500))
    choices = selections(data)
    vp9 = [item for item in choices if item.quality.codec == "vp9"]
    assert {item.quality.key for item in vp9} == {
        "video_2160p30_vp9_webm",
        "video_2160p30_vp9_webm_hdr10",
    }
    assert "HDR10" in next(item.quality.label for item in vp9 if item.video["format_id"] == "337")


def test_premium_bitrate_does_not_replace_the_standard_video_option():
    data = info()
    data["formats"].extend(
        [
            fmt("248", 1080, "vp9", tbr=3000),
            fmt("616", 1080, "vp9", tbr=5500, format_note="Premium"),
        ]
    )
    choices = selections(data)
    assert {item.quality.key for item in choices if item.quality.codec == "vp9"} == {
        "video_2160p30_vp9_webm",
        "video_1080p30_vp9_webm",
        "video_1080p30_vp9_webm_premium",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "BaW_jenozKc"},
        {"_type": "playlist"},
        {"is_live": True},
        {"live_status": "is_upcoming"},
        {"live_status": "post_live"},
    ],
)
def test_mismatched_ids_and_incomplete_live_videos_are_rejected(changes):
    with pytest.raises(DownloadError):
        read_media({**info(), **changes}, IDENTITY)


async def test_resolve_refreshes_both_urls_and_router_uses_youtube():
    calls = []

    async def extract(url):
        calls.append(url)
        data = copy.deepcopy(info())
        for item in data["formats"]:
            if item.get("url"):
                item["url"] = item["url"].replace("old", str(len(calls)))
        return data

    downloader = YouTubeDownloader(
        SimpleNamespace(extract=extract, proxy="", available=all_available)
    )
    router = DownloaderRouter({"youtube": downloader})
    media = await router.inspect(f"https://youtu.be/{IDENTITY}")
    quality = next(item for item in media.qualities if item.height == 1080)
    source = await router.resolve(media, quality)
    assert "expires=2" in source.url and "expires=2" in source.audio_url
    assert calls == [PAGE, PAGE]
    with pytest.raises(DownloadError):
        await downloader.resolve(media, replace(quality, key="missing"))


async def test_a_disappeared_quality_is_not_silently_downgraded():
    data = info()

    async def extract(url):
        return data

    downloader = YouTubeDownloader(
        SimpleNamespace(extract=extract, proxy="", available=all_available)
    )
    media = await downloader.inspect(PAGE)
    quality = next(item for item in media.qualities if item.height == 2160)
    data["formats"] = [fmt("18", 360, "avc1", "mp4a.40.2")]
    with pytest.raises(DownloadError):
        await downloader.resolve(media, quality)


async def test_unreachable_preferred_urls_do_not_hide_an_available_quality():
    data = info()
    data["formats"].extend(
        [
            fmt("137-alt", 1080, "avc1", tbr=2500),
            fmt("139", audio="mp4a.40.2", abr=48, language="en", language_preference=10),
        ]
    )
    sampled = []

    async def available(items):
        sampled.append({item["format_id"] for item in items})
        return {item["url"] for item in items if item["format_id"] not in {"137", "140"}}

    async def extract(url):
        return data

    downloader = YouTubeDownloader(SimpleNamespace(extract=extract, proxy="", available=available))
    media = await downloader.inspect(PAGE)
    quality = next(item for item in media.qualities if item.key == "video_1080p30_h264_mp4")
    source = await downloader.resolve(media, quality)
    assert "137-alt?" in source.url and "139?" in source.audio_url
    assert "137-alt" in sampled[0] and "139" in sampled[0]
    # Resolve probes alternatives of the requested quality and its compatible audio.
    assert sampled[1] == {"137", "137-alt", "139", "140"}


@pytest.mark.parametrize(
    "diagnostic",
    [
        b"Sign in to confirm you're not a bot https://secret.example/?token=SECRET",
        b"Private video cookie=SECRET",
        b"HTTP Error 429 token=SECRET",
        b"HTTP Error 403 SECRET",
        b"No module named yt_dlp SECRET",
        b"JavaScript runtime SECRET",
    ],
)
def test_extractor_errors_never_expose_tokens_or_raw_urls(diagnostic):
    error = extraction_error(diagnostic)
    assert isinstance(error, DownloadError)
    assert "SECRET" not in str(error) and "https://" not in str(error)


async def test_extractor_process_does_not_block_and_is_reaped_on_cancellation(monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []
    started = asyncio.Event()

    async def spawn(*args, **kwargs):
        assert "--ignore-config" in args and "--skip-download" in args
        process = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(YouTubeClient().extract(PAGE))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.01)  # The loop remains responsive while the worker waits.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


async def test_extractor_timeout_kills_worker(monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(DownloadError, match="طولانی"):
        await YouTubeClient(timeout=0.05).extract(PAGE)
    assert processes[0].returncode is not None


async def test_small_cdn_ranges_deliver_the_whole_file():
    payload = b"0123456789abcdef"
    ranges = []

    def response(request):
        value = request.headers["Range"]
        ranges.append(value)
        start, stop = map(int, value.removeprefix("bytes=").split("-"))
        return httpx.Response(
            206,
            content=payload[start : stop + 1],
            headers={
                "Content-Range": f"bytes {start}-{stop}/{len(payload)}",
            },
        )

    source = Source("https://cdn.example/audio", "progressive", size_bytes=16, chunk_size=5)
    progress = TransferProgress()
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        result = b"".join([chunk async for chunk in media_chunks(http, source, None, "", progress)])
    assert result == payload and progress.download_done and progress.total == 16
    assert ranges == ["bytes=0-4", "bytes=5-9", "bytes=10-14", "bytes=15-15"]


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"Content-Range": "bytes 1-4/16"}, b"abcd"),
        ({"Content-Range": "bytes 0-4/16"}, b"ab"),
        ({"Content-Range": "bytes 0-4/17"}, b"abcde"),
        ({"Content-Range": "bytes 0-4/*"}, b"abcde"),
    ],
)
async def test_wrong_or_truncated_cdn_ranges_are_rejected(headers, body):
    source = Source("https://cdn.example/audio", "progressive", size_bytes=16, chunk_size=5)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(206, headers=headers, content=body),
        )
    ) as http:
        with pytest.raises(DownloadError):
            _ = [chunk async for chunk in media_chunks(http, source, None, "")]


def test_scoped_cookies_do_not_leak_to_other_hosts():
    item = fmt(
        "140",
        audio="mp4a.40.2",
        cookies=(
            "cdn=right; Domain=.cdn.example; Path=/; Secure; "
            "account=SECRET; Domain=.youtube.com; Path=/; Secure"
        ),
    )
    assert source_headers(item)["Cookie"] == "cdn=right"


async def test_cdn_samples_exclude_denied_or_invalid_media(monkeypatch):
    original = httpx.AsyncClient

    def respond(request):
        if request.url.path == "/denied":
            return httpx.Response(403)
        if request.url.path == "/invalid":
            return httpx.Response(200, content=b"<html>not a video</html>")
        if request.url.path == "/hls":
            return httpx.Response(200, content=b"#EXTM3U\n#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n")
        if request.url.path == "/live":
            return httpx.Response(200, content=b"#EXTM3U\n#EXTINF:1,\nseg.ts\n")
        return httpx.Response(206, content=b"\x00\x00\x00\x18ftypisom" + b"0" * 100)

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    items = [fmt(name) for name in ("ok", "denied", "invalid")]
    items += [fmt(name, protocol="m3u8_native") for name in ("hls", "live")]
    available = await YouTubeClient().available(items)
    assert available == {items[0]["url"], items[3]["url"]}


async def test_long_finite_hls_is_read_without_a_truncating_range_header(monkeypatch):
    original = httpx.AsyncClient
    manifest = b"#EXTM3U\n" + b"#EXTINF:2,\nsegment.ts\n" * 600 + b"#EXT-X-ENDLIST\n"

    def respond(request):
        assert "range" not in request.headers
        return httpx.Response(200, content=manifest)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond))
    )
    item = fmt("long-hls", protocol="m3u8_native")
    assert await YouTubeClient().available([item]) == {item["url"]}


async def test_one_cdn_timeout_preserves_other_verified_formats(monkeypatch):
    original = httpx.AsyncClient

    def respond(request):
        if request.url.path == "/broken":
            raise httpx.ConnectTimeout("SECRET signed URL")
        return httpx.Response(206, content=b"\x00\x00\x00\x18ftypisom" + bytes(100))

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond))
    )
    good, broken = fmt("good"), fmt("broken")
    assert await YouTubeClient().available([broken, good]) == {good["url"]}


async def test_cdn_rate_limit_remains_visible_even_when_another_format_is_healthy(monkeypatch):
    original = httpx.AsyncClient

    def respond(request):
        if request.url.path == "/limited":
            return httpx.Response(429)
        return httpx.Response(206, content=b"\x00\x00\x00\x18ftypisom" + bytes(100))

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond))
    )
    with pytest.raises(DownloadError) as failure:
        await YouTubeClient().available([fmt("good"), fmt("limited")])
    assert failure.value.status == 429


def test_webpage_login_challenge_is_not_misclassified_as_an_age_restriction():
    error = extraction_error(b"Downloading webpage. Sign in to confirm you're not a bot SECRET")
    assert error.code == "youtube_login_required" and "SECRET" not in str(error)


async def test_successful_worker_with_no_formats_reports_its_safe_challenge_reason(monkeypatch):
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        assert "--no-warnings" not in args
        return await original(
            sys.executable,
            "-c",
            "import json,sys; print(json.dumps({'id':'jNQXAC9IVRw','formats':[]})); "
            "print('WARNING: n challenge solving failed SECRET',file=sys.stderr)",
            **kwargs,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(DownloadError) as failure:
        await YouTubeClient().extract(PAGE)
    assert failure.value.code == "youtube_js_challenge_failed"
    assert "SECRET" not in str(failure.value)


async def test_quality_requires_both_cdn_streams_to_be_accessible():
    async def extract(url):
        return info()

    async def available(items):
        return {item["url"] for item in items if item["vcodec"] != "none"}

    downloader = YouTubeDownloader(SimpleNamespace(extract=extract, proxy="", available=available))
    media = await downloader.inspect(PAGE)
    assert [quality.height for quality in media.qualities] == [360]


async def test_resolve_keeps_the_inspecting_account():
    calls = []

    def client(name):
        async def extract(url):
            calls.append(name)
            return info()

        return SimpleNamespace(extract=extract, proxy="", available=all_available)

    downloader = YouTubeDownloader(client("guest"), {"one": client("one"), "two": client("two")})
    first = await downloader.inspect(PAGE)
    second = await downloader.inspect(PAGE)
    await downloader.resolve(first, first.qualities[0])
    assert (first.account_id, second.account_id) == ("one", "two")
    assert calls == ["one", "two", "one"]
