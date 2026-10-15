import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest

from bot.state.menus import MenuStore
from bot.transfers.streaming import media_chunks
from downloader_bot.downloaders.instagram.client import (
    InstagramBrowserTransport,
    InstagramClient,
    instagram_session,
)
from downloader_bot.downloaders.instagram.downloader import InstagramDownloader
from downloader_bot.downloaders.instagram.handler import handle_instagram_link
from downloader_bot.downloaders.instagram.parser import (
    cdn_url,
    content_path,
    dash_formats,
    find_media,
    media_id,
    mp4_duration,
    mp4_info,
    mp4_payload_before_metadata,
    page_media,
    read_media,
)
from downloader_bot.downloaders.router import DownloaderRouter, extract_instagram_url
from schemas.media import DownloadError, SiteHTTPError, Source

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


def test_named_embed_callback_reads_only_its_json_and_nested_dash_manifest():
    node = find_media(fixture(), "Chunk8-jurw")
    page = (
        "<script>window.__additionalDataLoaded('/p/Chunk8-jurw/',"
        + json.dumps(node)
        + ");</script>"
    )
    assert page_media(page, "Chunk8-jurw") == node
    assert page_media(page.replace(");</script>", ");evil()</script>"), "Chunk8-jurw") is None
    nested = dict(node)
    nested["dash_info"] = {"video_dash_manifest": nested.pop("video_dash_manifest")}
    assert read_media(nested, PAGE, "Chunk8-jurw") == read_media(node, PAGE, "Chunk8-jurw")


async def test_browser_transport_decodes_once_closes_samples_and_never_sends_account_cookies_to_cdn():
    calls, closed = [], []

    class Browser:
        async def request(self, method, url, **options):
            calls.append((url, options))
            # curl-cffi streams decoded bytes but retains the original response headers.
            headers = httpx.Headers({"content-encoding": "gzip", "content-length": "40"})
            if url.endswith("instagram.com/"):
                headers["set-cookie"] = "csrftoken=PRIVATE; Domain=.instagram.com; Path=/"
            parts = (
                [b'<script>["LSD",[],{"token":"bootstrap"}]</script>']
                if url.endswith("instagram.com/")
                else [MP4 + bytes(2000), b"unread tail"]
            )

            async def chunks():
                for part in parts:
                    yield part

            async def close():
                closed.append(url)

            return SimpleNamespace(
                status_code=200, headers=headers, aiter_content=chunks, aclose=close
            )

    async with httpx.AsyncClient(transport=InstagramBrowserTransport(Browser())) as http:
        client = InstagramClient(http, "sessionid=PRIVATE")
        assert await client._bootstrap() == "bootstrap"
        assert await client._bootstrap() == "bootstrap"
        assert len(await client.sample("https://scontent.cdninstagram.com/video.mp4", PAGE)) == 1024
    assert len(calls) == 2 and len(closed) == 2
    assert "cookie" not in calls[1][1]["headers"]
    assert calls[0][1]["data"] is None and calls[0][1]["allow_redirects"] is False


async def test_browser_sessions_are_independent_and_close_with_their_provider():
    proxy = "http://127.0.0.1:8088"
    async with instagram_session(proxy) as first:
        async with instagram_session() as second:
            first.cookies.set("sessionid", "PRIVATE", domain=".instagram.com")
            assert not second.cookies
            assert first._transport.session is not second._transport.session
            assert first._transport.session.proxies == {"all": proxy}
            assert not second._transport.session.proxies
        assert second.is_closed and not first.is_closed
    assert first.is_closed


@pytest.mark.parametrize(
    "proxy",
    ["socks5://localhost:8080", "https://localhost:8080", "http://", "http://a:0", "http://a:bad"],
)
async def test_invalid_instagram_proxy_is_rejected_without_network(proxy):
    with pytest.raises(DownloadError, match="INSTAGRAM_PROXY"):
        async with instagram_session(proxy):
            pytest.fail("An invalid route must not open a provider session")


async def test_account_failover_keeps_proxy_for_progressive_and_dash_resolution(monkeypatch):
    proxy = "http://route-user:route-password@127.0.0.1:8088"

    def respond(request):
        if request.url.host.endswith("cdninstagram.com"):
            assert "cookie" not in request.headers
            return httpx.Response(206, content=sample_mp4(audio=True))
        if request.url.path.startswith("/p/"):
            return httpx.Response(200, text="")
        return httpx.Response(200, json=fixture("audio_reel"))

    async def guest_content(url):
        raise DownloadError("No guest data", code="instagram_empty_media")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        guest = InstagramClient(http)
        account = InstagramClient(http, "sessionid=PRIVATE", proxy=proxy)
        monkeypatch.setattr(guest, "content", guest_content)
        provider = InstagramDownloader(guest, {"working-route": account})
        media = await provider.inspect("https://www.instagram.com/reel/DZwzHDsiVlk/")
        assert media.account_id == "working-route"
        assert {q.protocol for q in media.qualities} == {"progressive", "dash"}
        for protocol in ("progressive", "dash"):
            quality = next(q for q in media.qualities if q.protocol == protocol)
            source = await provider.resolve(media, quality)
            assert source.proxy == proxy
            assert all(key.lower() != "cookie" for key in source.headers)
            assert all(key.lower() != "cookie" for key in (source.audio_headers or {}))
            assert "route-password" not in repr(source)


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
        if request.url.path.endswith("/info/"):
            return httpx.Response(404)
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


@pytest.mark.parametrize("invalidate", ["expired", "cdn_denied", "changed_identity"])
async def test_resolution_snapshot_is_bounded_fresh_and_refreshes_invalid_sources(
    monkeypatch, invalidate
):
    now = 100.0
    calls = 0
    reject_old = False
    node = find_media(fixture(), "Chunk8-jurw")
    media, formats = read_media(node, PAGE, "Chunk8-jurw")

    async def content(client, url):
        nonlocal calls
        calls += 1
        fresh = (
            replace(media, content_id="replacement")
            if calls == 2 and invalidate == "changed_identity"
            else media
        )
        return fresh, [
            replace(fmt, source=replace(fmt.source, url=fmt.source.url + f"&generation={calls}"))
            for fmt in formats
        ]

    async def sample(url, page_url, **kwargs):
        if reject_old and "generation=1" in url:
            raise SiteHTTPError(403, "Expired URL")
        return sample_mp4(audio=False)

    monkeypatch.setattr(
        "downloader_bot.downloaders.instagram.downloader.time",
        SimpleNamespace(monotonic=lambda: now),
    )
    async with httpx.AsyncClient() as http:
        client = InstagramClient(http)
        monkeypatch.setattr(client, "sample", sample)
        downloader = InstagramDownloader(client)
        monkeypatch.setattr(downloader, "_media", content)
        quality = next(q for q in media.qualities if "_mp4_" in q.key)
        selected = await downloader._selected(client, media, formats, quality)
        quality = selected[0].quality
        media = replace(media, qualities=(quality,))
        first, second = await asyncio.gather(
            downloader.resolve(media, quality), downloader.resolve(media, quality)
        )
        assert calls == 1 and first.url == second.url
        assert "generation=1" in first.url
        if invalidate == "cdn_denied":
            reject_old = True
        else:
            now += 16
        if invalidate == "changed_identity":
            with pytest.raises(DownloadError, match="تغییر"):
                await downloader.resolve(media, quality)
        else:
            third = await downloader.resolve(media, quality)
            assert "generation=2" in third.url
        assert calls == 2
        assert len(downloader._resolution_cache) <= 128


@pytest.mark.parametrize("failure", ["status", "timeout", "malformed"])
async def test_unavailable_optional_apis_do_not_hide_the_working_shortcode_query(failure):
    queried = []

    def respond(request):
        queried.append(request.url.path)
        if request.url.host.endswith("cdninstagram.com"):
            return httpx.Response(206, content=sample_mp4(audio=False))
        if request.url.path.startswith("/p/"):
            return httpx.Response(200, text="<html></html>")
        if request.url.path.endswith("get_ruling_for_content/"):
            if failure == "timeout":
                raise httpx.ConnectTimeout("optional endpoint timeout")
            return httpx.Response(503)
        if request.url.path == "/api/graphql":
            if failure == "timeout":
                raise httpx.ConnectTimeout("optional endpoint timeout")
            if failure == "malformed":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(403)
        assert request.url.path == "/graphql/query"
        return httpx.Response(200, json=fixture())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await InstagramDownloader(InstagramClient(http)).inspect(PAGE)
    assert media.content_id == "Chunk8-jurw" and media.qualities
    assert "/graphql/query" in queried


@pytest.mark.parametrize("page_status", [200, 403, 404])
async def test_reported_reel_null_root_response_still_tries_the_shortcode_api(page_status):
    code = "DeR9eU8idi0"
    node = find_media(fixture(), "Chunk8-jurw")
    node = {**node, "code": code, "pk": media_id(code)}
    paths = []

    def respond(request):
        paths.append(request.url.path)
        if request.url.host.endswith("cdninstagram.com"):
            return httpx.Response(206, content=sample_mp4(audio=False))
        if request.url.path.startswith("/p/"):
            return httpx.Response(page_status, text="<html></html>")
        if request.url.path == "/api/graphql":
            return httpx.Response(200, json={"data": {"xig_polaris_media": None}})
        if request.url.path == "/graphql/query":
            return httpx.Response(200, json={"data": {"xdt_shortcode_media": node}})
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = InstagramDownloader(InstagramClient(http))
        media = await provider.inspect(f"https://www.instagram.com/reel/{code}/?dlrf=tracking")
        assert media.content_id == code and media.qualities
        assert (await provider.resolve(media, media.qualities[0])).url
    assert "/graphql/query" in paths


async def test_authorized_product_without_shortcode_matches_numeric_identity_before_web_page():
    code = "DeR9eU8idi0"
    node = find_media(fixture(), "Chunk8-jurw")
    node.pop("code", None)
    node.pop("shortcode", None)
    node["pk"] = media_id(code) + "_123"

    def respond(request):
        if request.url.host.endswith("cdninstagram.com"):
            assert "cookie" not in request.headers
            return httpx.Response(206, content=sample_mp4(audio=False))
        assert request.url.path == f"/api/v1/media/{media_id(code)}/info/"
        assert request.headers["cookie"] == "sessionid=private"
        return httpx.Response(200, json={"items": [node]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await InstagramDownloader(InstagramClient(http, "sessionid=private")).inspect(
            f"https://www.instagram.com/reel/{code}/"
        )
    assert media.content_id == code and media.qualities
    assert find_media({"items": [{**node, "pk": "123"}]}, code) is None


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


async def test_a_directly_configured_authorized_client_can_inspect_and_resolve_a_story():
    cookie = "sessionid=private"
    visits = []
    story = {
        "pk": "123",
        "media_type": 1,
        "image_versions2": {
            "candidates": [
                {
                    "url": "https://scontent.cdninstagram.com/story.jpg",
                    "width": 1080,
                    "height": 1920,
                }
            ]
        },
    }

    def respond(request):
        visits.append(request.url.path)
        if request.url.host.endswith("cdninstagram.com"):
            assert "cookie" not in request.headers
            return httpx.Response(200, content=b"\xff\xd8\xffimage")
        assert request.headers["cookie"] == cookie
        if request.url.path.endswith("web_profile_info/"):
            return httpx.Response(200, json={"data": {"user": {"id": "456", "username": "user"}}})
        return httpx.Response(200, json={"reels": {"456": {"items": [story]}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = InstagramDownloader(InstagramClient(http, cookie))
        media = await downloader.inspect("https://instagram.com/stories/user/123/")
        source = await downloader.resolve(media, media.qualities[0])
    assert media.content_id == "story_123"
    assert source.url.endswith("story.jpg")


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
    assert "file" not in responses[0] and responses[0]["link_preview"] is False
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
    assert [sum(len(row) for row in page) for page in replies] == [50, 50, 20]
    buttons = [button for page in replies for row in page for button in row]
    assert [int(button.type.data.rsplit(b":", 1)[1]) for button in buttons] == list(range(120))


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


async def test_rotated_authorized_cookie_is_used_for_graphql_and_never_reaches_cdn():
    visits = []

    def respond(request):
        visits.append(request.url.path)
        if request.url.host.endswith("cdninstagram.com"):
            assert "cookie" not in request.headers and "x-csrftoken" not in request.headers
            return httpx.Response(206, content=sample_mp4(audio=False))
        if request.url.path.endswith("/info/"):
            return httpx.Response(404)
        if request.url.path.startswith("/p/"):
            return httpx.Response(
                200,
                text='<script>["LSD",[],{"token":"lsd"}]</script>',
                headers={"set-cookie": "csrftoken=rotated; Domain=.instagram.com; Path=/"},
            )
        assert "csrftoken=rotated" in request.headers["cookie"]
        assert "csrftoken=old" not in request.headers["cookie"]
        if request.url.path == "/api/graphql":
            assert request.headers["x-csrftoken"] == "rotated"
            return httpx.Response(200, json=fixture())
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await InstagramDownloader(
            InstagramClient(http, "sessionid=private; csrftoken=old")
        ).inspect(PAGE)
    assert media.qualities and "/api/graphql" in visits


@pytest.mark.parametrize("failure", ["api_401", "login_json", "page_login"])
async def test_optional_login_failure_does_not_prevent_shortcode_recovery(failure):
    visits = []

    def respond(request):
        visits.append(request.url.path)
        if request.url.path.endswith("/info/"):
            if failure == "api_401":
                return httpx.Response(401)
            if failure == "login_json":
                return httpx.Response(200, json={"status": "fail", "message": "login_required"})
            return httpx.Response(404)
        if request.url.path.startswith("/p/"):
            if failure == "page_login":
                return httpx.Response(302, headers={"location": "/accounts/login/"})
            return httpx.Response(200, text="<html></html>")
        if request.url.path == "/graphql/query":
            form = parse_qs(request.content.decode())
            assert set(form) == {"doc_id", "variables", "server_timestamps"}
            assert "x-fb-friendly-name" not in request.headers
            return httpx.Response(200, json=fixture())
        return httpx.Response(200, json={"data": {"xig_polaris_media": None}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = InstagramClient(http, "sessionid=private")
        node, _, code = await client.content(PAGE)
        assert find_media(node, code)
        assert client._cooldown_until == 0
    assert "/graphql/query" in visits


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"status": "fail", "message": "login_required SECRET"}, "instagram_login_required"),
        ({"errors": [{"message": "execution error SECRET"}]}, "instagram_graphql_unavailable"),
    ],
)
async def test_exhausted_alternatives_report_safe_specific_access_error(payload, code):
    def respond(request):
        if request.url.path.startswith("/p/"):
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError) as failure:
            await InstagramClient(http).content(PAGE)
    assert failure.value.code == code
    assert "SECRET" not in str(failure.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": [{"message": "execution error", "code": 1675004}]},
        {"errors": [{"message": "Rate limit exceeded"}]},
        {"status": "fail", "message": "Please wait a few minutes"},
    ],
)
async def test_json_rate_limits_stop_all_alternatives_and_enter_cooldown(payload):
    visits = []

    def respond(request):
        visits.append(request.url.path)
        if request.url.path.startswith("/p/"):
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = InstagramClient(http)
        for _ in range(2):
            with pytest.raises(SiteHTTPError) as failure:
                await client.content(PAGE)
            assert failure.value.status == 429
    assert visits == ["/p/Chunk8-jurw/", "/api/v1/web/get_ruling_for_content/"]


async def test_current_web_document_recovers_reported_reel_and_keeps_tokens_scoped():
    code = "DeR9eU8idi0"
    node = {**find_media(fixture(), "Chunk8-jurw"), "code": code, "pk": media_id(code)}
    documents = []

    def respond(request):
        if request.url.host.endswith("cdninstagram.com"):
            assert "cookie" not in request.headers and "x-fb-lsd" not in request.headers
            return httpx.Response(206, content=sample_mp4(audio=False))
        if request.url.path.endswith("/info/"):
            return httpx.Response(401)
        if request.url.path.startswith("/p/"):
            return httpx.Response(
                200,
                text='<script>["LSD",[],{"token":"lsd-secret"}]</script>'
                '<script>["DTSGInitData",[],{"token":"dtsg-secret"}]</script>',
            )
        if request.url.path.endswith("get_ruling_for_content/"):
            return httpx.Response(200, json={"status": "ok"})
        form = parse_qs(request.content.decode())
        documents.append(form["doc_id"][0])
        if documents[-1] == "27130156389949648":
            return httpx.Response(200, json={"data": {"xig_polaris_media": None}})
        assert documents[-1] == "27830990013244856"
        assert request.url.path == "/api/graphql"
        assert request.headers["x-fb-friendly-name"] == "PolarisPostRootQuery"
        assert request.headers["x-fb-lsd"] == "lsd-secret"
        assert form["fb_dtsg"] == ["dtsg-secret"]
        assert json.loads(form["variables"][0]) == {
            "shortcode": code,
            "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": True,
        }
        return httpx.Response(200, json={"data": {"xdt_shortcode_media": node}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = InstagramDownloader(InstagramClient(http, "sessionid=secret"))
        media = await provider.inspect(f"https://www.instagram.com/reel/{code}/?dlrf=tracking")
        assert media.content_id == code and media.qualities
        source = await provider.resolve(media, media.qualities[0])
        assert not any("secret" in value for value in source.headers.values())
    assert documents == ["27130156389949648", "27830990013244856"] * 2


async def test_failed_bootstrap_is_coalesced_briefly_then_retries_after_recovery(monkeypatch):
    from downloader_bot.downloaders.instagram import client as module

    clock, calls = [100.0], []
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def respond(request):
        calls.append(request)
        if clock[0] < 110:
            raise httpx.ConnectTimeout("SECRET")
        return httpx.Response(200, text='<script>["LSD",[],{"token":"recovered"}]</script>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = InstagramClient(http)
        assert await asyncio.gather(*(client._bootstrap() for _ in range(50))) == [""] * 50
        assert len(calls) == 1
        clock[0] += 11
        assert await client._bootstrap() == "recovered"
        assert len(calls) == 2


async def test_all_connection_failures_report_network_error_without_raw_details():
    def respond(request):
        raise httpx.ConnectError("https://SECRET.invalid/cookie")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError) as failure:
            await InstagramClient(http).content(PAGE)
    assert failure.value.code == "instagram_connection_failed"
    assert "SECRET" not in str(failure.value)


async def test_http_forbidden_is_preserved_when_all_optional_endpoints_are_blocked(caplog):
    def respond(request):
        return httpx.Response(403, text="SECRET cookie=PRIVATE")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError) as failure:
            await InstagramClient(http).content(PAGE)
    assert failure.value.code == "instagram_http_forbidden"
    assert "SECRET" not in caplog.text and "PRIVATE" not in caplog.text
    rows = [record.msg for record in caplog.records if isinstance(record.msg, dict)]
    assert any(row.get("http_status") == 403 for row in rows)


async def test_graphql_schema_failure_is_logged_without_claiming_network_rejection(caplog):
    def respond(request):
        if request.url.path.startswith("/p/"):
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, json={"errors": [{"message": "Cannot query SECRET field"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError) as failure:
            await InstagramClient(http).content(PAGE)
    rows = [record.msg for record in caplog.records if isinstance(record.msg, dict)]
    assert failure.value.code == "instagram_graphql_unavailable"
    assert any(row.get("failure_category") == "query_schema" for row in rows)
    assert "SECRET" not in caplog.text


async def test_content_deadline_is_a_recoverable_provider_error(monkeypatch):
    async def expired(self, url):
        raise TimeoutError

    monkeypatch.setattr(InstagramClient, "_content", expired)
    with pytest.raises(DownloadError) as failure:
        await InstagramClient(None).content(PAGE)
    assert failure.value.code == "instagram_extraction_timeout"


async def test_stalled_guest_is_cancelled_and_authorized_account_still_gets_a_turn(monkeypatch):
    from downloader_bot.downloaders.instagram import downloader as module

    monkeypatch.setattr(module, "ACCOUNT_ATTEMPT_SECONDS", 0.02)
    cancelled = asyncio.Event()

    async def stalled(url):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def healthy(url):
        return find_media(fixture(), "Chunk8-jurw"), PAGE, "Chunk8-jurw"

    async def available(client, media, formats):
        return formats

    provider = InstagramDownloader(
        SimpleNamespace(cookie=None, content=stalled),
        {"authorized": SimpleNamespace(content=healthy)},
    )
    monkeypatch.setattr(provider, "_available", available)
    media = await asyncio.wait_for(provider.inspect(PAGE), 1)
    assert cancelled.is_set() and media.account_id == "authorized" and media.qualities


async def test_authorized_browser_api_does_not_wait_for_a_blocked_homepage():
    visits = []

    class Browser:
        async def request(self, method, url, **options):
            visits.append(url)
            assert url.endswith(f"/api/v1/media/{media_id('Chunk8-jurw')}/info/")
            assert "sessionid=PRIVATE" in options["headers"]["cookie"]

            async def chunks():
                yield json.dumps(fixture()).encode()

            async def close():
                pass

            return SimpleNamespace(
                status_code=200, headers=httpx.Headers(), aiter_content=chunks, aclose=close
            )

    async with httpx.AsyncClient(transport=InstagramBrowserTransport(Browser())) as http:
        node, _, code = await InstagramClient(http, "sessionid=PRIVATE").content(PAGE)
    assert find_media(node, code) and len(visits) == 1


def test_explicit_guest_transport_profile_is_opt_in_and_validated():
    from curl_cffi import CurlHttpVersion, CurlOpt

    from downloader_bot.downloaders.instagram.client import browser_options

    assert browser_options({}) == {}
    assert browser_options(
        {"INSTAGRAM_HTTP_VERSION": "3", "INSTAGRAM_DOH_URL": "https://dns.google/dns-query"}
    ) == {
        CurlOpt.HTTP_VERSION: CurlHttpVersion.V3ONLY,
        CurlOpt.DOH_URL: "https://dns.google/dns-query",
    }
    with pytest.raises(ValueError):
        browser_options({"INSTAGRAM_HTTP_VERSION": "2"})
    with pytest.raises(ValueError):
        browser_options({"INSTAGRAM_DOH_URL": "http://SECRET:password@localhost"})


async def test_bounded_account_attempts_rotate_so_later_healthy_accounts_are_not_starved(
    monkeypatch,
):
    async def rejected(url):
        raise DownloadError("login", code="instagram_login_required")

    async def healthy(url):
        return find_media(fixture(), "Chunk8-jurw"), PAGE, "Chunk8-jurw"

    async def available(client, media, formats):
        return formats

    provider = InstagramDownloader(
        SimpleNamespace(cookie=None, content=rejected),
        {
            "bad1": SimpleNamespace(content=rejected),
            "bad2": SimpleNamespace(content=rejected),
            "healthy": SimpleNamespace(content=healthy),
        },
    )
    monkeypatch.setattr(provider, "_available", available)
    with pytest.raises(DownloadError):
        await provider.inspect(PAGE)
    assert (await provider.inspect(PAGE)).account_id == "healthy"


def timed_sample():
    sample = sample_mp4()
    movie = bytes(12) + (1000).to_bytes(4, "big") + (11700).to_bytes(4, "big")
    movie = (len(movie) + 8).to_bytes(4, "big") + b"mvhd" + movie
    body = movie + sample[len(MP4) + 8 :]
    return MP4 + (len(body) + 8).to_bytes(4, "big") + b"moov" + body


async def test_public_resolver_preserves_identity_and_accepted_snapshot_serves_concurrent_choices(
    monkeypatch,
):
    requests, native_calls = [], []
    target = "https://scontent.cdninstagram.com/full.mp4"
    original_client = httpx.AsyncClient

    def resolve(request):
        requests.append(request)
        assert json.loads(request.content) == {"url": PAGE, "downloadMode": "auto"}
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(
            200, json={"status": "redirect", "url": target, "filename": "instagram_Chunk8-jurw.mp4"}
        )

    async def rejected(url):
        native_calls.append(url)
        raise DownloadError("native refused", code="instagram_graphql_unavailable")

    async def sample(url, page, **options):
        assert url == target
        return timed_sample()

    async with original_client() as http:
        client = InstagramClient(http, resolver_url="https://resolver.example")
        monkeypatch.setattr(client, "_content", rejected)
        monkeypatch.setattr(client, "sample", sample)
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **options: original_client(transport=httpx.MockTransport(resolve), **options),
        )
        provider = InstagramDownloader(client)
        media = await provider.inspect(PAGE + "?tracking=discarded")
        assert media.content_id == "Chunk8-jurw" and media.duration == 12
        sources = await asyncio.gather(
            *(provider.resolve(media, media.qualities[0]) for _ in range(1000))
        )
    assert {source.url for source in sources} == {target}
    assert all(source.duration == 12 and source.require_audio for source in sources)
    assert len(native_calls) == len(requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "picker",
            "url": "https://scontent.cdninstagram.com/full.mp4",
            "filename": "instagram_Chunk8-jurw.mp4",
        },
        {
            "status": "redirect",
            "url": "https://evil.example/full.mp4",
            "filename": "instagram_Chunk8-jurw.mp4",
        },
        {
            "status": "redirect",
            "url": "https://scontent.cdninstagram.com/full.mp4",
            "filename": "instagram_AnotherCode.mp4",
        },
    ],
)
async def test_public_resolver_rejects_other_identity_and_non_origin_downloads(
    monkeypatch, payload
):
    original_client = httpx.AsyncClient
    async with original_client() as http:
        client = InstagramClient(http, resolver_url="https://resolver.example")
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **options: original_client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
                **options,
            ),
        )
        with pytest.raises(DownloadError) as failure:
            await client._resolve_public("Chunk8-jurw")
    assert failure.value.code == "instagram_resolver_unavailable"


async def test_origin_throttle_does_not_trigger_public_resolver(monkeypatch):
    async def limited(url):
        raise SiteHTTPError(429, "rate limited")

    async def forbidden(*args, **kwargs):
        pytest.fail("Origin throttling must stop alternatives")

    async with httpx.AsyncClient() as http:
        client = InstagramClient(http, resolver_url="https://resolver.example")
        monkeypatch.setattr(client, "_content", limited)
        monkeypatch.setattr(client, "_resolve_public", forbidden)
        with pytest.raises(SiteHTTPError):
            await client.content(PAGE)


def test_resolver_movie_duration_is_read_from_a_bounded_complete_header():
    assert mp4_duration(timed_sample()) == 11.7
    assert mp4_duration(timed_sample()[:-1]) == 0
    assert mp4_duration(bytes(20)) == 0


async def test_native_page_with_media_needs_no_homepage_bootstrap():
    visits = []

    class Browser:
        async def request(self, method, url, **options):
            visits.append(url)
            assert url == PAGE

            async def chunks():
                yield (
                    '<script type="application/json">' + json.dumps(fixture()) + "</script>"
                ).encode()

            return SimpleNamespace(
                status_code=200, headers=httpx.Headers(), aiter_content=chunks, aclose=AsyncMock()
            )

    async with httpx.AsyncClient(transport=InstagramBrowserTransport(Browser())) as http:
        node, _, code = await InstagramClient(http).content(PAGE)
    assert find_media(node, code) and visits == [PAGE]


async def test_complete_metadata_closes_stream_without_reading_payload():
    closed = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            header = timed_sample()
            yield header + bytes(16384 - len(header))
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(206, stream=Body()))
    ) as http:
        data = await asyncio.wait_for(
            InstagramClient(http).sample(
                "https://scontent.cdninstagram.com/full.mp4", PAGE, metadata=True
            ),
            1,
        )
    assert mp4_info(data) == (480, 854, True)
    assert mp4_duration(data) == 11.7 and len(data) == 16384 and closed.is_set()


@pytest.mark.parametrize("large_header", [False, True])
async def test_mdat_prefix_uses_small_tail_and_expands_only_for_large_movie_index(large_header):
    def box(tag, body):
        return (len(body) + 8).to_bytes(4, "big") + tag + body

    movie = timed_sample()[len(MP4) :]
    if large_header:
        movie = box(b"moov", box(b"free", bytes(128 * 1024)) + movie[8:])
    blob = MP4 + box(b"mdat", bytes(3 * 1024 * 1024)) + movie
    ranges, consumed = [], []

    class Body(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data

        async def __aiter__(self):
            for offset in range(0, len(self.data), 8192):
                chunk = self.data[offset : offset + 8192]
                consumed.append(len(chunk))
                yield chunk

    def respond(request):
        value = request.headers["range"]
        ranges.append(value)
        if value.startswith("bytes=-"):
            selected = blob[-int(value.partition("=-")[2]) :]
        else:
            selected = blob[: int(value.partition("-")[2]) + 1]
        return httpx.Response(206, stream=Body(selected))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = InstagramClient(http)
        front = await client.sample(
            "https://scontent.cdninstagram.com/full.mp4", PAGE, metadata=True
        )
        assert mp4_payload_before_metadata(front) and len(front) == 16384
        tail = await client.sample(
            "https://scontent.cdninstagram.com/full.mp4", PAGE, metadata=True, tail=True
        )
    assert mp4_info(tail) == (480, 854, True) and mp4_duration(tail) == 11.7
    assert ranges[:2] == ["bytes=0-1048575", "bytes=-65536"]
    if large_header:
        assert ranges[2:] == ["bytes=-1048576"]
    else:
        assert len(ranges) == 2 and sum(consumed) == 81920


async def test_probe_batch_coalesces_one_thousand_followers_and_ends_before_selection(monkeypatch):
    client = InstagramClient(None)
    sample = AsyncMock(return_value=timed_sample())
    monkeypatch.setattr(client, "_sample", sample)
    url = "https://scontent.cdninstagram.com/full.mp4"
    with client.probe_batch():
        rows = await asyncio.gather(*(client.sample(url, PAGE, metadata=True) for _ in range(1000)))
        assert all(row == timed_sample() for row in rows)
        assert sample.await_count == 1
    assert client._probe_batch.get() is None
    await client.sample(url, PAGE, metadata=True)
    assert sample.await_count == 2


async def test_probe_batch_shares_rate_failure_without_retrying_it(monkeypatch):
    client = InstagramClient(None)
    sample = AsyncMock(side_effect=SiteHTTPError(429, "rate limited"))
    monkeypatch.setattr(client, "_sample", sample)
    with client.probe_batch():
        rows = await asyncio.gather(
            *(
                client.sample("https://scontent.cdninstagram.com/full.mp4", PAGE)
                for _ in range(1000)
            ),
            return_exceptions=True,
        )
    assert sample.await_count == 1
    assert all(isinstance(row, SiteHTTPError) and row.status == 429 for row in rows)


async def test_probe_batch_has_byte_and_entry_bounds_and_is_session_owned(monkeypatch):
    client, other = InstagramClient(None), InstagramClient(None)
    monkeypatch.setattr(client, "_sample", AsyncMock(return_value=bytes(1024 * 1024)))
    monkeypatch.setattr(other, "_sample", AsyncMock(return_value=b"other-session"))
    with client.probe_batch():
        state = client._probe_batch.get()
        for index in range(12):
            await client.sample(f"https://scontent.cdninstagram.com/{index}.mp4", PAGE)
        assert state["bytes"] == 4 * 1024 * 1024 and len(state["cache"]) == 4
        assert (
            await other.sample("https://scontent.cdninstagram.com/11.mp4", PAGE) == b"other-session"
        )
        monkeypatch.setattr(client, "_sample", AsyncMock(side_effect=DownloadError("missing")))
        for index in range(200):
            with pytest.raises(DownloadError):
                await client.sample(f"https://scontent.cdninstagram.com/error{index}.mp4", PAGE)
        assert state["bytes"] <= 4 * 1024 * 1024 and len(state["cache"]) <= 128
    assert client._probe_batch.get() is None and other._probe_batch.get() is None


async def test_shared_dash_audio_is_probed_once_per_discovery(monkeypatch):
    media, formats = read_media(
        find_media(fixture("audio_reel"), "DZwzHDsiVlk"), PAGE, "DZwzHDsiVlk"
    )
    formats = [fmt for fmt in formats if fmt.source.audio_url]
    client = InstagramClient(None)
    sample = AsyncMock(return_value=MP4)
    monkeypatch.setattr(client, "_sample", sample)
    provider = InstagramDownloader(client)
    assert len(await provider._available(client, media, formats)) == len(formats)
    audio = formats[0].source.audio_url
    assert sum(call.args[0] == audio for call in sample.call_args_list) == 1
    assert sample.await_count == len(formats) + 1
    await provider._available(client, media, formats[:1])
    assert sum(call.args[0] == audio for call in sample.call_args_list) == 2


async def test_native_guest_deadline_cancels_work_before_verified_public_fallback(monkeypatch):
    client = InstagramClient(None, resolver_url="https://resolver.example")
    monkeypatch.setattr(client, "native_timeout", 0.01)
    cancelled = asyncio.Event()

    async def stalled(url):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(client, "_content", stalled)
    recovered = (fixture(), PAGE, "Chunk8-jurw")
    fallback = AsyncMock(return_value=recovered)
    monkeypatch.setattr(client, "_resolve_public", fallback)
    assert await asyncio.wait_for(client.content(PAGE), 1) == recovered
    assert cancelled.is_set() and fallback.await_count == 1


async def test_preferred_resolver_is_identity_scoped_and_failure_allows_native_once(monkeypatch):
    client = InstagramClient(None, resolver_url="https://resolver.example")
    recovered = (fixture(), PAGE, "Chunk8-jurw")
    native = AsyncMock(side_effect=DownloadError("native rejected"))
    fallback = AsyncMock(return_value=recovered)
    monkeypatch.setattr(client, "_content", native)
    monkeypatch.setattr(client, "_resolve_public", fallback)
    assert await client.content(PAGE) == recovered
    assert await client.content(PAGE) == recovered
    assert native.await_count == 1 and fallback.await_count == 2
    native.side_effect, native.return_value = None, recovered
    other = "https://www.instagram.com/p/DZwzHDsiVlk/"
    assert await client.content(other) == recovered
    assert native.await_count == 2 and fallback.await_count == 2
    fallback.side_effect = DownloadError("resolver unavailable")
    assert await client.content(PAGE) == recovered
    assert native.await_count == 3 and fallback.await_count == 3
    assert "Chunk8-jurw" not in client._resolver_preferred


async def test_public_resolver_429_is_not_hidden_by_the_previous_native_failure(monkeypatch):
    original = httpx.AsyncClient
    visits = []

    def limited(request):
        visits.append(request)
        return httpx.Response(429)

    client = InstagramClient(None, resolver_url="https://resolver.example")
    monkeypatch.setattr(client, "_content", AsyncMock(side_effect=DownloadError("native rejected")))
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(limited), **kw)
    )
    for _ in range(2):
        with pytest.raises(SiteHTTPError) as failure:
            await client.content(PAGE)
        assert failure.value.status == 429
    assert len(visits) == 1


async def test_unrelated_selected_posts_do_not_share_a_network_refresh_lock(monkeypatch):
    client = InstagramClient(None)
    provider = InstagramDownloader(client)
    media, formats = read_media(find_media(fixture(), "Chunk8-jurw"), PAGE, "Chunk8-jurw")
    blocked, released = asyncio.Event(), asyncio.Event()

    async def content(actual, url):
        if url == PAGE:
            blocked.set()
            await released.wait()
            return media, formats
        return replace(media, content_id="DZwzHDsiVlk"), formats

    monkeypatch.setattr(provider, "_media", content)
    first = asyncio.create_task(provider._resolution_media(client, media))
    await blocked.wait()
    try:
        other = replace(
            media, content_id="DZwzHDsiVlk", page_url="https://www.instagram.com/p/DZwzHDsiVlk/"
        )
        result = await asyncio.wait_for(provider._resolution_media(client, other), 1)
        assert result[0].content_id == other.content_id
    finally:
        released.set()
        await first
    assert provider._refresh_locks.count == 0


@pytest.mark.parametrize("value", [0, 36, float("nan"), float("inf")])
def test_native_budget_is_bounded(value):
    with pytest.raises(ValueError):
        InstagramClient(None, native_timeout=value)


async def test_cancelled_probe_batch_releases_every_waiter_without_cache_leak(monkeypatch):
    client = InstagramClient(None)
    entered, closed = asyncio.Event(), asyncio.Event()
    state = None

    async def stalled(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def request():
        nonlocal state
        with client.probe_batch():
            state = client._probe_batch.get()
            await asyncio.gather(
                *(
                    client.sample("https://scontent.cdninstagram.com/full.mp4", PAGE)
                    for _ in range(1000)
                )
            )

    monkeypatch.setattr(client, "_sample", stalled)
    task = asyncio.create_task(request())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and state["locks"].count == 0
    assert client._probe_batch.get() is None and not state["cache"]


async def test_page_lsd_can_feed_graphql_without_a_homepage_request():
    visits = []

    class Browser:
        async def request(self, method, url, **options):
            visits.append(url)
            assert url != "https://www.instagram.com/"
            if url == PAGE:
                body = b'<script>["LSD",[],{"token":"page-lsd"}]</script>'
            elif method == "POST":
                assert options["headers"]["x-fb-lsd"] == "page-lsd"
                body = json.dumps(fixture()).encode()
            else:
                body = b"{}"

            async def chunks():
                yield body

            return SimpleNamespace(
                status_code=200, headers=httpx.Headers(), aiter_content=chunks, aclose=AsyncMock()
            )

    async with httpx.AsyncClient(transport=InstagramBrowserTransport(Browser())) as http:
        node, _, code = await InstagramClient(http).content(PAGE)
    assert find_media(node, code) and len(visits) == 3


async def test_successful_resolver_preference_has_a_fixed_bound(monkeypatch):
    client = InstagramClient(None, resolver_url="https://resolver.example")
    monkeypatch.setattr(client, "_content", AsyncMock(side_effect=DownloadError("native rejected")))
    monkeypatch.setattr(
        client, "_resolve_public", AsyncMock(return_value=(fixture(), PAGE, "code"))
    )
    for index in range(200):
        await client.content(f"https://www.instagram.com/p/A{index:011d}/")
    assert len(client._resolver_preferred) == 128
