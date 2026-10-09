import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from telethon import types

from downloader_bot.downloaders.instagram.client import InstagramClient
from downloader_bot.downloaders.instagram.downloader import InstagramDownloader
from downloader_bot.downloaders.instagram.handler import handle_instagram_link
from downloader_bot.downloaders.instagram.parser import (
    cdn_url,
    content_path,
    dash_formats,
    find_media,
    media_id,
    mp4_info,
    page_media,
    read_media,
)
from downloader_bot.downloaders.router import DownloaderRouter
from downloader_bot.menus import MenuStore
from downloader_bot.models import DownloadError, SiteHTTPError, Source
from downloader_bot.streaming import media_chunks
from downloader_bot.urls import extract_instagram_url

FIXTURES = Path(__file__).parent / "fixtures" / "instagram"
PAGE = "https://www.instagram.com/p/Chunk8-jurw/"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 12


def fixture(name="muted_reel"):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


def sample_mp4(width=480, height=854, audio=True):
    def box(name, value):
        return (len(value) + 8).to_bytes(4, "big") + name + value

    tkhd = bytes(76) + (width << 16).to_bytes(4, "big") + (height << 16).to_bytes(4, "big")
    video = box(b"trak", box(b"tkhd", tkhd) + box(b"mdia", box(b"hdlr", bytes(8) + b"vide")))
    sound = box(b"trak", box(b"mdia", box(b"hdlr", bytes(8) + b"soun"))) if audio else b""
    return MP4 + box(b"moov", video + sound)


@pytest.mark.parametrize(
    "url",
    [
        "instagram.com/p/Chunk8-jurw/",
        "www.instagram.com/reel/Chunk8-jurw/",
        "http://m.instagram.com/reels/Chunk8-jurw/",
        "این لینک (https://instagram.com/tv/Chunk8-jurw/).",
    ],
)
def test_instagram_url_normalization(url):
    assert extract_instagram_url(url).startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "https://instagram.com.evil.org/p/a",
        "https://evil.instagram.com/p/a",
        "https://instagram.com@evil.org/p/a",
        "https://evil.org/instagram.com/p/a",
        "me@instagram.com",
        "https://instagram.com:9000/p/a",
    ],
)
def test_instagram_lookalikes_are_rejected(url):
    assert extract_instagram_url(url) is None


@pytest.mark.parametrize(
    "path, expected",
    [
        ("p/Chunk8-jurw/", ("post", "Chunk8-jurw")),
        ("instagram/reel/Chunk8-jurw/", ("post", "Chunk8-jurw")),
        ("share/reel/Abc/", ("share", "Abc")),
        ("stories/user/123/", ("story", "user/123")),
    ],
)
def test_supported_content_paths(path, expected):
    assert content_path("https://www.instagram.com/" + path) == expected
    assert media_id("Chunk8-jurw") == "2913440072144448240"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/",
        "https://www.instagram.com/explore/",
        "https://www.instagram.com/reels/audio/123/",
        "http://instagram.com/p/a",
    ],
)
async def test_invalid_pages_are_rejected_before_network(url):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network"))
    ) as http:
        with pytest.raises(DownloadError):
            await InstagramDownloader(InstagramClient(http)).inspect(url)


def test_live_response_shapes_keep_real_dimensions_audio_and_each_album_item():
    node = find_media(fixture("audio_reel"), "DZwzHDsiVlk")
    media, formats = read_media(node, "https://www.instagram.com/p/DZwzHDsiVlk/", "DZwzHDsiVlk")
    best = max((f for f in formats if f.source.protocol == "dash"), key=lambda f: f.quality.width)
    assert (best.quality.width, best.quality.height) == (1440, 2560)
    assert best.source.audio_url and best.source.require_audio
    assert media.duration == 23
    node = find_media(fixture("carousel"), "BQ0eAlwhDrw")
    _, formats = read_media(node, PAGE, "BQ0eAlwhDrw")
    assert {f.quality.duration for f in formats} == {5, 32, 7}
    assert len({f.quality.key.split("_")[0] for f in formats}) == 3
    assert all("بخش" in f.quality.label for f in formats)


def test_relay_and_encoded_embedded_data_are_read_without_executing_scripts():
    payload = fixture()
    nested = {"require": [["RelayPrefetchedStreamCache", [], {"__bbox": {"result": payload}}]]}
    assert page_media(
        '<script type="application/json">' + json.dumps(nested) + "</script>", "Chunk8-jurw"
    )
    assert page_media(
        "<script>window._sharedData=" + json.dumps(payload) + ";</script>", "Chunk8-jurw"
    )
    assert find_media({"gql_data": json.dumps(payload)}, "Chunk8-jurw")
    assert page_media('<script>alert("video_url")</script>', "Chunk8-jurw") is None
    assert find_media(payload, "AnotherCode") is None


@pytest.mark.parametrize("change", ["segments", "drm", "dynamic", "entities", "missing_audio"])
def test_incomplete_unsafe_or_silent_dash_is_not_offered_as_complete_audio_video(change):
    node = find_media(fixture("audio_reel"), "DZwzHDsiVlk")
    xml = node["video_dash_manifest"]
    if change == "segments":
        xml = xml.replace("<BaseURL>", '<SegmentTemplate media="chunk-$Number$.m4s"/><BaseURL>')
    elif change == "drm":
        xml = xml.replace("<BaseURL>", "<ContentProtection/><BaseURL>")
    elif change == "dynamic":
        xml = xml.replace('type="static"', 'type="dynamic"')
    elif change == "entities":
        xml = '<!DOCTYPE MPD [<!ENTITY x "unsafe">]>' + xml
    elif change == "missing_audio":
        xml = xml.replace('mimeType="audio/mp4"', 'mimeType="audio/unknown"')
    assert dash_formats(xml, "item", True, "")[0] == []


@pytest.mark.parametrize(
    "url",
    [
        "http://scontent.cdninstagram.com/v.mp4",
        "https://cdninstagram.com.evil.org/v.mp4",
        "https://127.0.0.1/v.mp4",
        "https://evil.org/v.mp4",
        "https://scontent.cdninstagram.com:9000/v.mp4",
    ],
)
def test_only_instagram_and_meta_cdns_are_accepted(url):
    assert cdn_url(url) is None


def test_bounded_mp4_reader_uses_rendition_dimensions_and_detects_audio():
    assert mp4_info(sample_mp4()) == (480, 854, True)
    assert mp4_info(sample_mp4(audio=False)) == (480, 854, False)
    assert mp4_info(sample_mp4()[:40]) == (None, None, None)


async def test_new_graphql_flow_refreshes_signed_urls_and_does_not_leak_cookies():
    calls = []
    generation = 0

    def respond(request):
        nonlocal generation
        calls.append(request)
        if request.url.host.endswith("cdninstagram.com"):
            assert "cookie" not in request.headers
            assert "x-csrftoken" not in request.headers
            assert request.headers["range"].startswith("bytes=")
            return httpx.Response(206, content=sample_mp4(audio=False))
        assert request.headers["cookie"] == "sessionid=private; csrftoken=secret"
        if request.url.path.startswith("/p/"):
            return httpx.Response(200, text='<script>["LSD",[],{"token":"fresh-lsd"}]</script>')
        if request.url.path.endswith("get_ruling_for_content/"):
            assert request.url.params["target_id"] == "2913440072144448240"
            return httpx.Response(200, json={"status": "ok"})
        assert request.url.path == "/api/graphql"
        assert request.headers["x-fb-lsd"] == "fresh-lsd"
        assert request.headers["x-csrftoken"] == "secret"
        assert (
            request.headers["x-fb-friendly-name"]
            == "PolarisLoggedOutDesktopWWWPostRootContentQuery"
        )
        assert request.headers["sec-fetch-mode"] == "cors"
        assert parse_qs(request.content.decode())["doc_id"] == ["27130156389949648"]
        generation += 1
        return httpx.Response(
            200,
            json=json.loads(json.dumps(fixture()).replace("expires=old", f"expires={generation}")),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = InstagramDownloader(
            InstagramClient(http, "sessionid=private; csrftoken=secret")
        )
        media = await downloader.inspect(PAGE)
        quality = next(q for q in media.qualities if "_mp4_" in q.key)
        assert (quality.width, quality.height) == (480, 854)
        source = await downloader.resolve(media, quality)
        assert "expires=2" in source.url
        assert "Cookie" not in source.headers
        assert media.content_id == "Chunk8-jurw"
        assert len([r for r in calls if r.url.path == "/api/graphql"]) == 2


async def test_unavailable_optional_apis_do_not_hide_the_working_shortcode_query():
    queried = []

    def respond(request):
        queried.append(request.url.path)
        if request.url.host.endswith("cdninstagram.com"):
            return httpx.Response(206, content=sample_mp4(audio=False))
        if request.url.path.startswith("/p/"):
            return httpx.Response(200, text="<html></html>")
        if request.url.path.endswith("get_ruling_for_content/"):
            return httpx.Response(503)
        if request.url.path == "/api/graphql":
            return httpx.Response(403)
        assert request.url.path == "/graphql/query"
        return httpx.Response(200, json=fixture())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await InstagramDownloader(InstagramClient(http)).inspect(PAGE)
    assert media.content_id == "Chunk8-jurw" and media.qualities
    assert "/graphql/query" in queried


async def test_rate_limit_has_cooldown_and_no_recursive_retry_storm():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, json={"message": "Please wait a few minutes"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = InstagramClient(http)
        for _ in range(2):
            with pytest.raises(SiteHTTPError):
                await client.content(PAGE)
    assert len(calls) == 1


async def test_offsite_redirect_is_rejected_before_contacting_the_host():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.org/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError):
            await InstagramClient(http, "sessionid=secret").content(PAGE)
    assert len(calls) == 1


async def test_guest_story_requires_an_authorized_session_before_any_request():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network"))
    ) as http:
        with pytest.raises(DownloadError, match="نشست"):
            await InstagramDownloader(InstagramClient(http)).inspect(
                "https://instagram.com/stories/user/123/"
            )


async def test_router_and_instagram_menu_use_the_shared_delivery_contract():
    node = find_media(fixture(), "Chunk8-jurw")
    media, _ = read_media(node, PAGE, "Chunk8-jurw")
    responses = []

    async def inspect(url):
        assert url == PAGE
        return media

    router = DownloaderRouter({"instagram": SimpleNamespace(inspect=inspect)})

    async def respond(text, **kwargs):
        responses.append(kwargs)

    event = SimpleNamespace(raw_text="لینک " + PAGE, sender_id=1, chat_id=2, respond=respond)
    await handle_instagram_link(event, SimpleNamespace(inspect=router.inspect), MenuStore())
    assert isinstance(responses[0]["file"], types.InputMediaPhotoExternal)
    assert responses[0]["buttons"][0][0].type.data.startswith(b"ig:")
    assert len(responses[0]["buttons"][0][0].type.data) <= 64


async def test_large_album_quality_menu_is_split_without_losing_choices():
    node = find_media(fixture(), "Chunk8-jurw")
    media, _ = read_media(node, PAGE, "Chunk8-jurw")
    media = replace(
        media,
        thumbnail=None,
        qualities=tuple(replace(media.qualities[0], key=str(i)) for i in range(120)),
    )
    replies = []

    async def inspect(_):
        return media

    async def respond(text, **kwargs):
        replies.append(kwargs["buttons"])

    event = SimpleNamespace(raw_text=PAGE, sender_id=1, chat_id=2, respond=respond)
    await handle_instagram_link(event, SimpleNamespace(inspect=inspect), MenuStore())
    assert list(map(len, replies)) == [50, 50, 20]
    assert replies[-1][-1][0].type.data.endswith(b":119")


async def test_network_waits_remain_nonblocking_and_cancellable():
    started = asyncio.Event()

    async def respond(request):
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        job = asyncio.create_task(InstagramClient(http).content(PAGE))
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(0)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job


async def test_jpeg_post_lists_each_real_size_and_resolves_a_direct_url():
    node = {
        "code": "photo",
        "pk": "123",
        "media_type": 1,
        "user": {"username": "user"},
        "image_versions2": {
            "candidates": [
                {
                    "url": "https://scontent.cdninstagram.com/large.jpg",
                    "width": 1080,
                    "height": 1350,
                },
                {"url": "https://scontent.cdninstagram.com/small.jpg", "width": 480, "height": 600},
            ]
        },
    }

    def respond(request):
        if request.url.host.endswith("cdninstagram.com"):
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0JPEG")
        return httpx.Response(
            200, text='<script type="application/json">' + json.dumps(node) + "</script>"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        d = InstagramDownloader(InstagramClient(http))
        m = await d.inspect("https://www.instagram.com/p/photo/")
        assert [(q.width, q.height) for q in m.qualities] == [(1080, 1350), (480, 600)]
        source = await d.resolve(m, m.qualities[0])
        assert source.protocol == "progressive" and source.url.endswith("large.jpg")


async def test_selected_dimensions_changing_do_not_reuse_a_different_quality():
    node = {
        "code": "video",
        "pk": "123",
        "has_audio": True,
        "media_type": 2,
        "video_versions": [{"type": 101, "url": "https://scontent.cdninstagram.com/v.mp4"}],
    }
    generation = 0

    def respond(request):
        nonlocal generation
        if request.url.host.endswith("cdninstagram.com"):
            return httpx.Response(206, content=sample_mp4(width=480 if generation == 1 else 720))
        generation += 1
        return httpx.Response(
            200, text='<script type="application/json">' + json.dumps(node) + "</script>"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        d = InstagramDownloader(InstagramClient(http))
        m = await d.inspect("https://www.instagram.com/p/video/")
        assert m.qualities[0].key.endswith("480x854")
        with pytest.raises(DownloadError, match="تغییر"):
            await d.resolve(m, m.qualities[0])


async def test_dash_remux_preserves_separate_video_and_audio(tmp_path):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    video, audio = tmp_path / "video.mp4", tmp_path / "audio.m4a"
    for output, arguments in [
        (video, ["-f", "lavfi", "-i", "color=c=blue:s=32x32:r=10", "-c:v", "libx264", "-an"]),
        (
            audio,
            ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-c:a", "aac", "-vn"],
        ),
    ]:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            *arguments,
            "-t",
            "0.5",
            str(output),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, diagnostic = await process.communicate()
        assert process.returncode == 0, diagnostic.decode(errors="replace")
    node = find_media(fixture("audio_reel"), "DZwzHDsiVlk")
    _, formats = read_media(node, PAGE, "DZwzHDsiVlk")
    quality = formats[0].quality
    async with httpx.AsyncClient() as http:
        data = b"".join(
            [
                chunk
                async for chunk in media_chunks(
                    http,
                    Source(str(video), "dash", audio_url=str(audio), require_audio=True),
                    quality,
                    ffmpeg,
                )
            ]
        )
    assert b"ftyp" in data[:32]
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
    _, diagnostic = await verify.communicate(data)
    assert verify.returncode == 0, diagnostic.decode(errors="replace")
