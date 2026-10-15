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
)
from schemas.media import DownloadError, SiteHTTPError

IDENTITY = "l6zflbTGNFQ"
PAGE = f"https://www.youtube.com/watch?v={IDENTITY}"


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
        if path.endswith("search.php"):
            assert request.url.params["q"] == IDENTITY
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
            assert request.url.params["link"] == PAGE
            return httpx.Response(200, json={"key": "fresh-request-nonce"})
        if path == "/v2/converter":
            body = json.loads(request.content)
            assert body["link"] == PAGE
            assert request.headers["key"] == "fresh-request-nonce"
            assert request.headers["origin"] == module.PUBLIC_ORIGIN
            assert not request.headers.get("cookie")
            assert "python-httpx" not in request.headers.get("user-agent", "")
            if state["status"] != 200:
                return httpx.Response(state["status"], json={})
            identity = "jNQXAC9IVRw" if state["wrong_id"] else IDENTITY
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
    assert not public_api[0]


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
