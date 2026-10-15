"""Independent TikTok URL, identity, fast metadata and source-refresh contracts."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from core.config import ConfigurationError
from downloader_bot.downloaders.router import DownloaderRouter
from downloader_bot.downloaders.tiktok.client import TikTokClient
from downloader_bot.downloaders.tiktok.downloader import TikTokDownloader
from downloader_bot.downloaders.tiktok.parser import read_native, read_resolver
from downloader_bot.downloaders.tiktok.urls import content_id, extract_url, media_url, normalize_url
from repositories.metadata import public_media
from schemas.media import DownloadError, Media, Quality

IDENTITY = "6718335390845095173"
PAGE = f"https://www.tiktok.com/@tester/video/{IDENTITY}"
CDN = "https://v16.tiktokcdn.com/"


def resolver_payload(identity=IDENTITY, **values):
    return {
        "code": 0,
        "data": {
            "id": identity,
            "title": "Example",
            "author": {"unique_id": "tester"},
            "duration": 10,
            "hdplay": CDN + "hd.mp4?token=private-hd",
            "play": CDN + "standard.mp4?token=private-play",
            "hd_size": 2000,
            "size": 3000,
            "music": CDN + "music.mp3",
            "music_info": {"duration": 11},
            "cover": CDN + "cover.jpg",
            **values,
        },
    }


def native_page(identity=IDENTITY, *, sigi=False, **values):
    item = {
        "id": identity,
        "desc": "Public post",
        "author": {"uniqueId": "tester"},
        "video": {"playAddr": CDN + "native.mp4", "width": 720, "height": 1280, "duration": 10},
        "music": {"playUrl": CDN + "music.mp3"},
        **values,
    }
    if sigi:
        payload, tag = {"ItemModule": {identity: item}}, "SIGI_STATE"
    else:
        payload, tag = (
            {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}}}},
            "__UNIVERSAL_DATA_FOR_REHYDRATION__",
        )
    return f'<html><script id="{tag}" type="application/json">{json.dumps(payload)}</script></html>'


@pytest.mark.parametrize(
    "url",
    [
        PAGE,
        PAGE + "?is_from_webapp=1&sender_device=pc#share",
        PAGE.replace("www.", "m."),
        f"https://www.tiktok.com/embed/v2/{IDENTITY}",
        f"https://www.tiktok.com/player/v1/{IDENTITY}",
        f"https://tiktok.com/@another/photo/{IDENTITY}/",
    ],
)
def test_stable_post_identity_strips_tracking_and_does_not_depend_on_author(url):
    assert content_id(url) == IDENTITY
    assert "?" not in normalize_url(url)
    assert TikTokDownloader(None).cache_key(url) == IDENTITY


@pytest.mark.parametrize(
    "url",
    [
        "https://vm.tiktok.com/ZMabcdef/",
        "https://vt.tiktok.com/ZSabcdef/",
        "https://www.tiktok.com/t/ZTabcdef/",
    ],
)
def test_short_links_are_aliases_and_never_permanent_content_id_catalog_keys(url):
    downloader = TikTokDownloader(None)
    assert downloader.cache_key(url).startswith("share:")
    assert downloader.catalog_key(url) is None
    assert extract_url("Shared " + url + "?sender=secret") == url


@pytest.mark.parametrize(
    "url",
    [
        "https://nottiktok.com/@x/video/" + IDENTITY,
        "https://tiktok.com.evil.test/@x/video/" + IDENTITY,
        "https://evil.test/tiktok.com/@x/video/" + IDENTITY,
        "https://user@www.tiktok.com/@x/video/" + IDENTITY,
        "https://www.tiktok.com:8443/@x/video/" + IDENTITY,
        "https://www.tiktok.com/@x",
        "file:///etc/passwd",
        "https://127.0.0.1/",
        "https://www.tiktok.com/@x/video/abc",
        "https://www.tiktok.com/@x/live",
    ],
)
def test_bad_inputs_are_rejected_before_metadata_requests(url):
    with pytest.raises(DownloadError):
        normalize_url(url)
    assert extract_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://v16.tiktokcdn.com/a",
        "https://v16.tiktokcdn.com.evil.test/a",
        "https://127.0.0.1/a",
        "https://localhost/a",
        "https://evil.test/a",
        "https://user:secret@v16.tiktokcdn.com/a",
        "https://www.tiktok.com/a",
        "https://v16.tiktokcdn.com:8443/a",
    ],
)
def test_media_urls_cannot_escape_the_provider_cdn_boundary(url):
    assert media_url(url) is None


def test_resolver_lists_declared_assets_in_one_payload_without_inventing_dimensions():
    catalog = read_resolver(resolver_payload(), PAGE)
    media = catalog.media
    assert media.content_id == IDENTITY and media.page_url == PAGE
    assert [q.key for q in media.qualities] == ["video_hd", "video_standard", "soundtrack"]
    assert all(q.width is None and q.height is None for q in media.qualities)
    assert catalog.sources["video_hd"].size_bytes == 2000
    assert catalog.sources["video_hd"].require_audio is True
    assert catalog.sources["soundtrack"].duration == 11


def test_duplicate_video_urls_are_one_quality_and_watermark_is_never_mislabeled():
    catalog = read_resolver(resolver_payload(play=CDN + "hd.mp4?token=private-hd"), PAGE)
    assert [q.key for q in catalog.media.qualities] == ["video_hd", "soundtrack"]
    catalog = read_resolver(
        resolver_payload(play=None, hdplay=None, wmplay=CDN + "watermark.mp4"), PAGE
    )
    assert catalog.media.qualities[0].key == "video_watermark"


def test_photos_keep_asset_identity_after_reordering_signed_url_refresh_and_host_rotation():
    payload = resolver_payload(images=[CDN + "image-a.jpg?one", CDN + "image-b.jpg?one"])
    first = read_resolver(payload, PAGE)
    payload["data"]["images"] = [
        "https://p16.tiktokcdn-us.com/image-b.jpg?two",
        "https://p16.tiktokcdn-us.com/image-a.jpg?two",
    ]
    second = read_resolver(payload, PAGE)

    def identity(c):
        return {q.key: urlsplit(q.endpoint).path for q in c.media.qualities}

    assert identity(first) == identity(second)
    assert "/photo/" in first.media.page_url
    assert [q.label for q in first.media.qualities[:2]] == ["عکس 1", "عکس 2"]


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"code": True}, {"code": 0}, {"code": -1, "msg": "private detail"}]
)
def test_malformed_or_rejected_responses_are_safe_failures(payload):
    with pytest.raises(DownloadError) as error:
        read_resolver(payload, PAGE)
    assert "private detail" not in str(error.value)


def test_mismatched_id_is_not_replaced_with_another_posts_video():
    with pytest.raises(DownloadError) as error:
        read_resolver(resolver_payload("6718335390845095199"), PAGE)
    assert error.value.code == "tiktok_identity_mismatch"
    with pytest.raises(DownloadError):
        read_native(native_page("6718335390845095199"), PAGE)


@pytest.mark.parametrize("sigi", [True, False])
def test_native_script_variants_have_the_same_stable_identity(sigi):
    catalog = read_native(native_page(sigi=sigi), PAGE)
    assert catalog.media.content_id == IDENTITY
    quality = catalog.media.qualities[0]
    assert quality.width == 720 and quality.height == 1280 and quality.duration == 10


def test_native_bitrate_formats_omit_unsupported_and_video_only_encodes():
    video = {
        "duration": 10,
        "width": 720,
        "height": 1280,
        "bitrateInfo": [
            {
                "CodecType": codec,
                "Bitrate": 1000000,
                "PlayAddr": {"UrlList": [CDN + path], "Width": 540, "Height": 960},
            }
            for codec, path in [
                ("h264", "video.mp4"),
                ("bytevc2", "vvc.mp4"),
                ("bytevc1", "media-video-hvc1/"),
            ]
        ],
    }
    catalog = read_native(native_page(video=video), PAGE)
    assert len(catalog.media.qualities) == 1
    assert catalog.media.qualities[0].width == 540
    assert catalog.media.qualities[0].codec == "h264"
    with pytest.raises(DownloadError):
        read_native(native_page(privateItem=True), PAGE)


async def test_successful_resolver_is_one_request_including_short_links_and_selection():
    requests = []

    async def respond(request):
        requests.append(request)
        assert request.method == "POST" and request.url.host == "www.tikwm.com"
        form = parse_qs(request.content.decode())
        assert form["hd"] == ["1"] and form["url"] == ["https://vm.tiktok.com/Zabcdef/"]
        assert "Cookie" not in request.headers and "Authorization" not in request.headers
        return httpx.Response(200, json=resolver_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), headers={"Authorization": "secret"}
    ) as http:
        http.cookies.set("sessionid", "private", domain="www.tikwm.com")
        downloader = TikTokDownloader(TikTokClient(http))
        media = await downloader.inspect("https://vm.tiktok.com/Zabcdef/?private_tracking=secret")
        for quality in media.qualities:
            source = await downloader.resolve(media, quality)
            assert "Cookie" not in source.headers and "Authorization" not in source.headers
        assert len(requests) == 1


async def test_concurrent_duplicate_requests_share_one_producer_without_blocking_distinct_posts():
    calls = []

    async def content(url):
        calls.append(url)
        await asyncio.sleep(0.01)
        return read_resolver(resolver_payload(content_id(url)), url)

    client = SimpleNamespace(content=content, timeout=6)
    downloader = TikTokDownloader(client)
    other = PAGE.replace(IDENTITY, "6718335390845095199")
    results = await asyncio.gather(
        *(downloader.inspect(PAGE) for _ in range(100)), downloader.inspect(other)
    )
    assert len(calls) == 2 and results[-1].content_id != IDENTITY
    assert downloader._locks.count == 0


async def test_expired_and_durable_menus_refresh_the_same_selected_asset_once(monkeypatch):
    clock, calls = [10.0], []
    monkeypatch.setattr(
        "downloader_bot.downloaders.tiktok.downloader.time.monotonic", lambda: clock[0]
    )

    def respond(request):
        calls.append(request)
        payload = resolver_payload()
        payload["data"]["hdplay"] = CDN + f"hd.mp4?renewal={len(calls)}"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = TikTokDownloader(TikTokClient(http))
        media = await downloader.inspect(PAGE)
        assert "renewal=1" in (await downloader.resolve(media, media.qualities[0])).url
        clock[0] = 56
        snapshot = public_media(media)
        stored = Media(
            **{**snapshot, "qualities": tuple(Quality(**q) for q in snapshot["qualities"])}
        )
        assert all(q.endpoint == "" for q in stored.qualities)
        sources = await asyncio.gather(
            *(downloader.resolve(stored, stored.qualities[0]) for _ in range(20))
        )
        assert all("renewal=2" in source.url for source in sources)
        assert len(calls) == 2


async def test_missing_selected_quality_is_an_explicit_error_not_a_lower_quality_substitute():
    catalog = read_resolver(resolver_payload(), PAGE)
    fresh = read_resolver(resolver_payload(hdplay=None), PAGE)
    downloader = TikTokDownloader(SimpleNamespace(content=AsyncMock(return_value=fresh), timeout=6))
    with pytest.raises(DownloadError) as error:
        await downloader.resolve(catalog.media, catalog.media.qualities[0])
    assert error.value.code == "tiktok_quality_unavailable"


async def test_native_fallback_is_bounded_and_rejects_redirects_outside_tiktok():
    visited = []

    def respond(request):
        visited.append(request.url.host)
        if request.url.host == "www.tikwm.com":
            return httpx.Response(503)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError):
            await TikTokClient(http).content(PAGE)
    assert visited == ["www.tikwm.com", "www.tiktok.com"]


async def test_resolver_failure_uses_native_state_without_a_browser_process():
    visited = []

    def respond(request):
        visited.append(request.method)
        return (
            httpx.Response(503)
            if request.method == "POST"
            else httpx.Response(200, text=native_page())
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await TikTokDownloader(TikTokClient(http)).inspect(PAGE)
    assert media.site == "tiktok" and visited == ["POST", "GET"]


async def test_rejected_short_token_retries_only_its_verified_canonical_url_without_page_fetch():
    visited = []

    def respond(request):
        visited.append((request.method, request.url.host))
        if request.url.host == "vm.tiktok.com":
            return httpx.Response(302, headers={"Location": PAGE + "?tracking=private"})
        assert request.method == "POST"
        url = parse_qs(request.content.decode())["url"][0]
        if url == PAGE:
            return httpx.Response(200, json=resolver_payload())
        return httpx.Response(200, json={"code": -1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await TikTokClient(http).content("https://vm.tiktok.com/Zabcdef/")
    assert media.media.content_id == IDENTITY
    assert visited == [
        ("POST", "www.tikwm.com"),
        ("GET", "vm.tiktok.com"),
        ("POST", "www.tikwm.com"),
    ]


async def test_known_post_redirect_cannot_fetch_or_publish_another_identity():
    visited = []

    def respond(request):
        visited.append(str(request.url))
        return httpx.Response(
            302, headers={"Location": PAGE.replace(IDENTITY, "6718335390845095199")}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError) as error:
            await TikTokClient(http, endpoint="").content(PAGE)
    assert error.value.code == "tiktok_identity_mismatch" and visited == [PAGE]


async def test_rate_limit_honors_cooldown_without_sleeping_or_retrying_the_same_method():
    posts = []

    def respond(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(429, headers={"Retry-After": "300"})
        return httpx.Response(200, text=native_page())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = TikTokClient(http)
        await client.content(PAGE)
        await client.content(PAGE)
    assert len(posts) == 1


async def test_cancellation_releases_http_and_per_identity_capacity():
    entered = asyncio.Event()

    async def respond(request):
        entered.set()
        await asyncio.Future()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = TikTokClient(http)
        downloader = TikTokDownloader(client)
        task = asyncio.create_task(downloader.inspect(PAGE))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert downloader._locks.count == 0
        assert client._slots._value == 4


async def test_untrusted_response_has_a_size_bound_and_is_closed():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
        )
    ) as http:
        client = TikTokClient(http)
        with pytest.raises(DownloadError) as error:
            await client._read(http, "POST", client.endpoint, limit=2 * 1024 * 1024, anonymous=True)
        assert error.value.code == "tiktok_response_limit"


@pytest.mark.parametrize("value", ["not-number", "nan", "inf", "0", "21"])
def test_invalid_timeout_configuration_is_safe(monkeypatch, value):
    monkeypatch.setenv("TIKTOK_METADATA_TIMEOUT_SECONDS", value)
    with pytest.raises(ConfigurationError) as error:
        TikTokClient.from_environment(None)
    assert error.value.fields == ("TIKTOK_METADATA_TIMEOUT_SECONDS",)


@pytest.mark.parametrize(
    "value",
    [
        "http://api.example.test/",
        "https://user:secret@api.example.test/",
        "https://127.0.0.1/api/",
        "https://localhost/api/",
        "https://api.example.test/?secret=value",
    ],
)
def test_invalid_resolver_configuration_cannot_leak_a_secret(value):
    with pytest.raises(ConfigurationError) as error:
        TikTokClient(None, endpoint=value)
    assert value not in str(error.value)


async def test_tiktok_route_and_stale_selection_identity_are_checked():
    provider = TikTokDownloader(SimpleNamespace(content=AsyncMock(), timeout=6))
    router = DownloaderRouter({"tiktok": provider})
    assert router.site_for(PAGE) == "tiktok"
    assert router.cache_key(PAGE) == "tiktok:" + IDENTITY
    media = read_resolver(resolver_payload(), PAGE).media
    with pytest.raises(DownloadError):
        await provider.resolve(replace(media, site="instagram"), media.qualities[0])
    with pytest.raises(DownloadError):
        await provider.resolve(media, replace(media.qualities[0], key="invented"))
    provider.client.content.assert_not_awaited()
