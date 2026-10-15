import asyncio
import json
import time
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest

from downloader_bot.downloaders.youtube import client as module
from downloader_bot.downloaders.youtube.client import YouTubeClient
from downloader_bot.downloaders.youtube.downloader import YouTubeDownloader
from downloader_bot.downloaders.youtube.parser import (
    public_download_url,
    public_metadata,
    public_video_info,
    watch_metadata,
)
from schemas.media import DownloadError, SiteHTTPError

IDENTITY = "l6zflbTGNFQ"
PAGE = f"https://www.youtube.com/watch?v={IDENTITY}"


def watch_page(identity=IDENTITY, **changes):
    details = {
        "videoId": identity,
        "title": "Exact page title",
        "author": "Exact author",
        "lengthSeconds": "2236",
        "isPrivate": False,
        "isLiveContent": False,
        **changes,
    }
    return (
        "<script>var ytInitialPlayerResponse = "
        + json.dumps(
            {
                "videoDetails": details,
                "playabilityStatus": {"status": "OK"},
            }
        )
        + ";</script>"
    )


def box(tag, payload):
    return (len(payload) + 8).to_bytes(4, "big") + tag + payload


def mp4_header(height=360, audio=True):
    tkhd = bytearray(84)
    tkhd[-44:-40] = (65536).to_bytes(4, "big")
    tkhd[-8:-4] = (height * 16 // 9 << 16).to_bytes(4, "big")
    tkhd[-4:] = (height << 16).to_bytes(4, "big")
    stsd = box(b"stsd", bytes(8) + box(b"avc1", bytes(78)))
    mdia = box(b"hdlr", bytes(8) + b"vide") + box(b"minf", box(b"stbl", stsd))
    video = box(b"trak", box(b"tkhd", tkhd) + box(b"mdia", mdia))
    sound = box(b"trak", box(b"mdia", box(b"hdlr", bytes(8) + b"soun")))
    return box(b"ftyp", b"isom" + bytes(12)) + box(b"moov", video + (sound if audio else b""))


@pytest.fixture
def public_api(monkeypatch):
    visits, options = [], []
    state = {"status": 200, "wrong_id": False, "silent": False, "collapse": False}
    original_client = httpx.AsyncClient

    def route(request):
        visits.append(request)
        path = request.url.path
        requested = state.get("identity", IDENTITY)
        page = f"https://www.youtube.com/watch?v={requested}"
        if request.url.host == "www.youtube.com":
            assert path == "/watch" and request.url.params["v"] == requested
            assert not request.headers.get("origin") and not request.headers.get("cookie")
            return httpx.Response(
                state.get("watch_status", 200 if state.get("watch") else 403),
                text=state.get("watch", ""),
            )
        if path.endswith("search.php"):
            assert request.url.params["q"] == requested
            if state.get("empty_search"):
                return httpx.Response(200, json={"items": []})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "jNQXAC9IVRw", "title": "Wrong video", "duration": "21:41"},
                        {
                            "id": IDENTITY,
                            "title": "Exact video",
                            "duration": "21:41",
                            "channelTitle": "Author",
                        },
                    ]
                },
            )
        if path == "/v2/sanity/key":
            assert request.url.params["link"] == page
            return httpx.Response(200, json={"key": "fresh-request-nonce"})
        if path == "/v2/converter":
            body = json.loads(request.content)
            assert body["link"] == page
            assert request.headers["key"] == "fresh-request-nonce"
            assert request.headers["origin"] == module.PUBLIC_ORIGIN
            assert not request.headers.get("cookie")
            assert "python-httpx" not in request.headers.get("user-agent", "")
            if state["status"] != 200:
                return httpx.Response(state["status"], json={})
            identity = "jNQXAC9IVRw" if state["wrong_id"] else requested
            rendition = body["videoQuality"] if body["format"] == "mp4" else "mp3"
            url = f"https://dl01.yt-dl.click/media?vid={identity}&r={rendition}&exp={int((time.time() + 60) * 1000)}"
            return httpx.Response(200, json={"status": "tunnel", "url": url})
        assert request.url.host == "dl01.yt-dl.click"
        assert not request.headers.get("key") and not request.headers.get("cookie")
        assert request.headers["referer"] == module.PUBLIC_ORIGIN + "/"
        rendition = request.url.params["r"]
        height = 360 if state["collapse"] else int(rendition) if rendition != "mp3" else 0
        body = (
            b"ID3" + bytes(100) if rendition == "mp3" else mp4_header(height, not state["silent"])
        )
        return httpx.Response(200, content=body)

    class Browser:
        def __init__(self, **values):
            options.append(values)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def request(self, method, url, **values):
            response = route(
                httpx.Request(method, url, headers=values["headers"], content=values["data"])
            )

            class Result:
                status_code = response.status_code
                headers = response.headers

                async def aiter_content(self):
                    yield response.content

                async def aclose(self):
                    pass

            return Result()

    def client(*args, **values):
        values.setdefault("transport", httpx.MockTransport(route))
        return original_client(*args, **values)

    monkeypatch.setattr(module, "BoundedSession", Browser)
    monkeypatch.setattr(module.httpx, "AsyncClient", client)
    return visits, options, state


@pytest.mark.parametrize("reason", ["youtube_login_required", "youtube_connection_failed"])
async def test_native_challenge_recovers_anonymous_menu_and_selected_sources(
    monkeypatch, public_api, reason
):
    async def rejected(self, url, player_clients=None):
        raise DownloadError(code=reason)

    monkeypatch.setattr(YouTubeClient, "_extract", rejected)
    visits, options, _ = public_api
    downloader = YouTubeDownloader(YouTubeClient(resolver_url="https://cnv.cx"))
    media = await downloader.inspect(PAGE + "&pp=tracking")
    assert media.account_id == "public_resolver" and media.title == "Exact video"
    assert media.duration == 1301 and len(media.qualities) == 4
    for quality in media.qualities:
        source = await downloader.resolve(media, quality)
        assert parse_qs(httpx.URL(source.url).query.decode())["vid"] == [IDENTITY]
        assert source.duration == 1301 and not quality.original
        assert source.size_probed
        assert source.require_audio == quality.mime_type.startswith("video/")
    assert sum(r.method == "POST" for r in visits) == 4
    assert sum(r.url.path == "/v2/sanity/key" for r in visits) == 1
    assert options and all(o["discard_cookies"] and not o["trust_env"] for o in options)


async def test_expired_callback_coalesces_one_thousand_refreshes_and_preserves_menu(public_api):
    visits, _, _ = public_api
    client = YouTubeClient(resolver_url="https://cnv.cx")
    media = await client.public_inspect(IDENTITY)
    quality = media.qualities[0]
    expiry, _, rows = client._public_cache[IDENTITY]
    old_quality, old_source = rows[quality.key]
    rows[quality.key] = (
        old_quality,
        replace(old_source, url=old_source.url.split("&exp=")[0] + "&exp=1"),
    )
    before = sum(r.method == "POST" for r in visits)
    sources = await asyncio.gather(*(client.public_resolve(media, quality) for _ in range(1000)))
    assert len({s.url for s in sources}) == 1
    assert sum(r.method == "POST" for r in visits) == before + 1
    assert (await client.public_inspect(IDENTITY)).qualities == media.qualities
    client._public_cache[IDENTITY] = (expiry - 100, media, rows)
    assert len((await client.public_inspect(IDENTITY)).qualities) == 4


async def test_concurrent_inspections_share_one_catalog(public_api):
    visits, _, _ = public_api
    client = YouTubeClient(resolver_url="https://cnv.cx")
    results = await asyncio.gather(*(client.public_inspect(IDENTITY) for _ in range(1000)))
    assert all(media == results[0] for media in results)
    assert sum(r.method == "POST" for r in visits) == 4


async def test_accepted_public_route_skips_native_workers_for_following_requests(
    monkeypatch, public_api
):
    attempts = []

    async def rejected(self, url, player_clients=None):
        attempts.append(url)
        raise DownloadError(code="youtube_login_required")

    monkeypatch.setattr(YouTubeClient, "_extract", rejected)
    provider = YouTubeDownloader(YouTubeClient(resolver_url="https://cnv.cx"))
    await provider.inspect(PAGE)
    assert len(attempts) == 1
    provider.client._public_cache.clear()
    await provider.inspect(PAGE)
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "status,headers,size",
    [
        (200, {}, None),
        (200, {"Content-Length": "123456"}, 123456),
        (206, {"Content-Range": "bytes 0-4095/123456"}, 123456),
    ],
)
async def test_public_probe_returns_after_complete_header_without_reading_media_tail(
    public_api, monkeypatch, status, headers, size
):
    original = module.httpx.AsyncClient

    class HeaderOnly(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield mp4_header()
            pytest.fail("A verified initialization header must not wait for media bytes")

    def factory(**options):
        options["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(status, headers=headers, stream=HeaderOnly())
        )
        return original(**options)

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    quality, source = await YouTubeClient(resolver_url="https://cnv.cx")._public_source(
        {"status": "tunnel", "url": f"https://dl01.yt-dl.click/media?vid={IDENTITY}"},
        IDENTITY,
        "mp4",
        "360",
        1301,
    )
    assert quality.height == 360 and source.size_probed
    assert source.size_bytes == size


@pytest.mark.parametrize("native_recovers", [False, True])
async def test_preferred_public_failure_permits_native_once_without_repeating_public_request(
    monkeypatch, public_api, native_recovers
):
    from unittest.mock import AsyncMock

    client = YouTubeClient(resolver_url="https://cnv.cx")
    media = await client.public_inspect(IDENTITY)
    provider = YouTubeDownloader(client)
    provider._public_preferred_until = time.monotonic() + 300
    calls = []

    async def failed_public(self, identity):
        calls.append(identity)
        raise DownloadError(code="youtube_public_unavailable")

    monkeypatch.setattr(YouTubeClient, "public_inspect", failed_public)
    native = (
        AsyncMock(return_value=media)
        if native_recovers
        else AsyncMock(side_effect=DownloadError(code="youtube_login_required"))
    )
    monkeypatch.setattr(provider, "_inspect", native)
    if native_recovers:
        assert await provider.inspect(PAGE) == media
    else:
        with pytest.raises(DownloadError) as failure:
            await provider.inspect(PAGE)
        assert failure.value.code == "youtube_public_unavailable"
    assert calls == [IDENTITY] and native.await_count == 1
    assert provider._public_preferred_until == 0


async def test_menu_uses_actual_dimensions_and_rejects_silent_video(public_api):
    _, _, state = public_api
    state["collapse"] = True
    media = await YouTubeClient(resolver_url="https://cnv.cx").public_inspect(IDENTITY)
    assert len(media.qualities) == 2
    assert media.qualities[0].height == 360
    state["silent"] = True
    media = await YouTubeClient(resolver_url="https://cnv.cx").public_inspect(IDENTITY)
    assert len(media.qualities) == 1 and media.qualities[0].codec == "mp3"


async def test_wrong_identity_is_rejected_before_cdn_request(public_api):
    visits, _, state = public_api
    state["wrong_id"] = True
    with pytest.raises(DownloadError, match="مسیر عمومی"):
        await YouTubeClient(resolver_url="https://cnv.cx").public_inspect(IDENTITY)
    assert not any(r.url.host == "dl01.yt-dl.click" for r in visits)


async def test_public_429_cools_down_without_request_storm(public_api):
    visits, _, state = public_api
    state["status"] = 429
    client = YouTubeClient(resolver_url="https://cnv.cx")
    with pytest.raises(SiteHTTPError):
        await client.public_inspect(IDENTITY)
    before = len(visits)
    with pytest.raises(SiteHTTPError):
        await client.public_inspect(IDENTITY)
    assert len(visits) == before
    assert sum(r.method == "POST" for r in visits) <= 2


@pytest.mark.parametrize(
    "reason", ["youtube_private_video", "youtube_age_restricted", "youtube_rate_limited"]
)
async def test_private_age_and_rate_failures_never_use_public_service(
    monkeypatch, public_api, reason
):
    async def rejected(self, url, player_clients=None):
        raise DownloadError(code=reason)

    monkeypatch.setattr(YouTubeClient, "_extract", rejected)
    with pytest.raises(DownloadError):
        await YouTubeDownloader(YouTubeClient(resolver_url="https://cnv.cx")).inspect(PAGE)
    assert all(request.url.host == "www.youtube.com" for request in public_api[0])


@pytest.mark.parametrize(
    "options", [{}, {"resolver_url": "https://cnv.cx", "cookies_file": "private.cookies"}]
)
async def test_disabled_and_authenticated_clients_cannot_use_public_service(public_api, options):
    client = YouTubeClient(**options)
    with pytest.raises(DownloadError) as failure:
        await client.public_inspect(IDENTITY)
    assert failure.value.code == "youtube_public_disabled" and not public_api[0]


@pytest.mark.parametrize(
    "url",
    [
        "http://dl01.yt-dl.click/media?vid=l6zflbTGNFQ",
        "https://yt-dl.click.evil.example/media?vid=l6zflbTGNFQ",
        "https://user:secret@dl01.yt-dl.click/media?vid=l6zflbTGNFQ",
        "https://dl01.yt-dl.click:443/media?vid=l6zflbTGNFQ",
        "https://dl01.yt-dl.click/media?vid=jNQXAC9IVRw",
        "https://dl01.yt-dl.click/media?vid=l6zflbTGNFQ&vid=jNQXAC9IVRw",
    ],
)
def test_public_destination_requires_exact_identity_and_trusted_https_host(url):
    assert public_download_url(url, IDENTITY) is None


@pytest.mark.parametrize("duration", ["", "live", "00:00", "21:61", "1:61:00", "1000:00", "nan"])
def test_public_metadata_requires_exact_id_and_complete_duration(duration):
    with pytest.raises(DownloadError):
        public_metadata({"items": [{"id": IDENTITY, "duration": duration}]}, IDENTITY)
    with pytest.raises(DownloadError):
        public_metadata({"items": [{"id": "jNQXAC9IVRw", "duration": "21:41"}]}, IDENTITY)


def test_mp4_probe_handles_bytearray_and_refuses_truncated_or_oversized_boxes():
    assert public_video_info(bytearray(mp4_header())) == (640, 360, True, "h264")
    assert public_video_info(mp4_header()[:40]) == (0, 0, False, "")
    assert public_video_info(b"\xff\xff\xff\xffmoov" + bytes(32)) == (0, 0, False, "")


async def test_unindexed_leading_dash_video_uses_exact_page_without_native_startup(
    monkeypatch, public_api
):
    visits, _, state = public_api
    identity = "-YVIEsTEZCw"
    state.update(identity=identity, watch=watch_page(identity), empty_search=True)

    async def worker(*args, **kwargs):
        pytest.fail("An origin-confirmed guest VOD must not wait for a native worker first")

    monkeypatch.setattr(YouTubeClient, "_extract", worker)
    provider = YouTubeDownloader(YouTubeClient(resolver_url="https://cnv.cx"))
    media = await provider.inspect(f"https://www.youtube.com/watch?v={identity}")
    assert media.content_id == identity and media.duration == 2236
    assert media.title == "Exact page title" and len(media.qualities) == 4
    assert not any(r.url.path.endswith("search.php") for r in visits)
    assert sum(r.url.host == "www.youtube.com" for r in visits) == 1
    for quality in media.qualities:
        source = await provider.resolve(media, quality)
        assert source.duration == 2236 and parse_qs(httpx.URL(source.url).query.decode())[
            "vid"
        ] == [identity]


@pytest.mark.parametrize(
    "changes",
    [
        {"videoId": "jNQXAC9IVRw"},
        {"isPrivate": True},
        {"isLiveContent": True},
        {"lengthSeconds": "0"},
        {"lengthSeconds": "NaN"},
        {"lengthSeconds": 2236},
        {"lengthSeconds": "12345678"},
        {"title": ""},
        {"author": None},
    ],
)
def test_watch_metadata_refuses_wrong_identity_private_live_and_incomplete_details(changes):
    assert watch_metadata(watch_page(**changes), IDENTITY) is None


def test_watch_metadata_uses_json_boundaries_and_does_not_certify_login_as_playable():
    text = watch_page(title='A title with } and "quotes"', author="Author")
    result = watch_metadata(text, IDENTITY)
    assert (
        result.title == 'A title with } and "quotes"'
        and result.duration == 2236
        and result.playable
    )
    assert watch_metadata(text[:-15], IDENTITY) is None
    assert not watch_metadata(text.replace('"OK"', '"LOGIN_REQUIRED"'), IDENTITY).playable


async def test_one_thousand_origin_metadata_followers_share_one_capped_request(public_api):
    visits, _, state = public_api
    state["watch"] = watch_page()
    client = YouTubeClient(resolver_url="https://cnv.cx")
    rows = await asyncio.gather(*(client.origin_metadata(IDENTITY) for _ in range(1000)))
    assert all(row == rows[0] and row.playable for row in rows)
    assert len(visits) == 1
    for index in range(200):
        client._remember_origin(str(index), None)
    assert len(client._origin_cache) == 128


async def test_origin_429_stops_fallback_and_is_cooled_down(public_api):
    visits, _, state = public_api
    state["watch_status"] = 429
    provider = YouTubeDownloader(YouTubeClient(resolver_url="https://cnv.cx"))
    for _ in range(2):
        with pytest.raises(SiteHTTPError):
            await provider.inspect(PAGE)
    assert len(visits) == 1 and visits[0].url.host == "www.youtube.com"


async def test_metadata_failure_cancels_slow_key_request_without_conversion(
    monkeypatch, public_api
):
    visits, _, state = public_api
    state["empty_search"] = True
    started, stopped = asyncio.Event(), asyncio.Event()

    async def key(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(YouTubeClient, "_public_key", key)
    with pytest.raises(DownloadError) as failure:
        await asyncio.wait_for(
            YouTubeClient(resolver_url="https://cnv.cx").public_inspect(IDENTITY), 1
        )
    assert failure.value.code == "youtube_public_metadata_unavailable"
    assert started.is_set() and stopped.is_set()
    assert not any(r.method == "POST" for r in visits)


@pytest.mark.parametrize(
    "override", [{"player_clients": "mweb"}, {"pot_base_url": "http://localhost:4416"}]
)
async def test_explicit_native_settings_keep_native_first(monkeypatch, public_api, override):
    from unittest.mock import AsyncMock

    _, _, state = public_api
    state["watch"] = watch_page()
    provider = YouTubeDownloader(YouTubeClient(resolver_url="https://cnv.cx", **override))
    native = AsyncMock(return_value="native result")
    monkeypatch.setattr(provider, "_inspect", native)
    assert await provider.inspect(PAGE) == "native result"
    native.assert_awaited_once()
    assert not public_api[0]


async def test_page_fast_route_failure_allows_one_native_recovery(monkeypatch, public_api):
    from unittest.mock import AsyncMock

    visits, _, state = public_api
    state.update(watch=watch_page(), status=503)
    provider = YouTubeDownloader(YouTubeClient(resolver_url="https://cnv.cx"))
    native = AsyncMock(return_value="native recovery")
    monkeypatch.setattr(provider, "_inspect", native)
    assert await provider.inspect(PAGE) == "native recovery"
    native.assert_awaited_once()
    assert provider._public_preferred_until == 0
    assert sum(r.url.host == "www.youtube.com" for r in visits) == 1


def mock_origin_transport(monkeypatch, handler):
    original = module.httpx.AsyncClient

    def client(**options):
        options["transport"] = httpx.MockTransport(handler)
        return original(**options)

    monkeypatch.setattr(module.httpx, "AsyncClient", client)


async def test_origin_probe_closes_after_complete_player_without_consuming_tail(monkeypatch):
    closed = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield watch_page().encode() + b" " * 65536
            pytest.fail("Complete exact-ID player metadata must not wait for the page tail")

        async def aclose(self):
            closed.set()

    mock_origin_transport(monkeypatch, lambda request: httpx.Response(200, stream=Body()))
    result = await YouTubeClient().origin_metadata(IDENTITY)
    assert result.duration == 2236 and closed.is_set()


async def test_origin_byte_cap_is_finite_and_failed_lookup_is_cached(monkeypatch):
    reads, visits = [], []
    closed = asyncio.Event()
    monkeypatch.setattr(module, "MAX_WATCH_BYTES", 65536)

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(3):
                reads.append(index)
                yield b" " * 65536

        async def aclose(self):
            closed.set()

    def handler(request):
        visits.append(request)
        return httpx.Response(200, stream=Body())

    mock_origin_transport(monkeypatch, handler)
    client = YouTubeClient()
    assert await client.origin_metadata(IDENTITY) is None
    assert await client.origin_metadata(IDENTITY) is None
    assert reads == [0, 1] and len(visits) == 1 and closed.is_set()


async def test_origin_timeout_closes_response_and_does_not_repeat_within_inspection(monkeypatch):
    visits = []
    closed = asyncio.Event()
    monkeypatch.setattr(module, "ORIGIN_METADATA_TIMEOUT", 0.03)

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            closed.set()

    def handler(request):
        visits.append(request)
        return httpx.Response(200, stream=Body())

    mock_origin_transport(monkeypatch, handler)
    client = YouTubeClient()
    assert await asyncio.wait_for(client.origin_metadata(IDENTITY), 1) is None
    assert await client.origin_metadata(IDENTITY) is None
    assert len(visits) == 1 and closed.is_set()


async def test_key_failure_cancels_in_progress_metadata(monkeypatch, public_api):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def metadata(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def key(*args):
        await started.wait()
        raise ValueError("Unavailable request key")

    monkeypatch.setattr(YouTubeClient, "_public_metadata", metadata)
    monkeypatch.setattr(YouTubeClient, "_public_key", key)
    with pytest.raises(DownloadError) as failure:
        await asyncio.wait_for(
            YouTubeClient(resolver_url="https://cnv.cx").public_inspect(IDENTITY), 1
        )
    assert failure.value.code == "youtube_public_unavailable" and stopped.is_set()


async def test_page_lookup_is_charged_to_existing_native_attempt_budget(monkeypatch):
    from unittest.mock import AsyncMock

    budgets, timeout = [], asyncio.timeout

    def tracked(seconds):
        budgets.append(seconds)
        return timeout(seconds)

    async def missing(*args):
        await asyncio.sleep(0.01)
        return None

    client = YouTubeClient(resolver_url="https://cnv.cx", native_timeout=1)
    provider = YouTubeDownloader(client)
    monkeypatch.setattr(YouTubeClient, "origin_metadata", missing)
    monkeypatch.setattr(provider, "_inspect", AsyncMock(return_value="native"))
    monkeypatch.setattr(asyncio, "timeout", tracked)
    assert await provider.inspect(PAGE) == "native"
    assert budgets[0] == 85 and 0 < budgets[1] < 1


async def test_origin_429_prevents_requests_already_waiting_for_transport_capacity(monkeypatch):
    visits = []

    async def handler(request):
        visits.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(429)

    mock_origin_transport(monkeypatch, handler)
    client = YouTubeClient()
    object.__setattr__(client, "_origin_slots", asyncio.Semaphore(1))
    results = await asyncio.gather(
        *(client.origin_metadata(f"{index:011d}") for index in range(32)),
        return_exceptions=True,
    )
    assert all(isinstance(result, SiteHTTPError) and result.status == 429 for result in results)
    assert len(visits) == 1
