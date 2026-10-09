import asyncio
import json

import httpx
import pytest
from test_parser import variant

from downloader_bot.downloaders.soundcloud.client import SoundCloudClient
from downloader_bot.downloaders.soundcloud.downloader import SoundCloudDownloader
from downloader_bot.models import DownloadError, Media


def track_page(transcodings, include_track=True):
    track = {
        "id": 123,
        "title": "Test",
        "kind": "track",
        "duration": 120000,
        "media": {"transcodings": transcodings},
    }
    entries = [{"hydratable": "apiClient", "data": {"id": "a" * 32}}]
    if include_track:
        entries.append({"hydratable": "sound", "data": track})
    return "window.__sc_hydration = " + json.dumps(entries) + ";", track


@pytest.mark.parametrize("failure_stage", ["inspection", "selection"])
async def test_failed_progressive_endpoint_falls_back_to_full_hls_without_changing_quality(
    failure_stage,
):
    progressive = variant("mp3_1_0", "progressive", url="https://api-v2.soundcloud.com/progressive")
    hls = variant("mp3_1_0", url="https://api-v2.soundcloud.com/hls")
    page, _ = track_page([progressive, hls])
    progressive_requests = 0

    def respond(request):
        nonlocal progressive_requests
        if request.url.host == "soundcloud.com":
            return httpx.Response(200, text=page)
        if request.url.path == "/progressive":
            progressive_requests += 1
            if failure_stage == "selection" and progressive_requests == 1:
                return httpx.Response(200, json={"url": "https://cdn.example/full.mp3"})
            return httpx.Response(404)
        return httpx.Response(200, json={"url": "https://cdn.example/full.m3u8"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = SoundCloudDownloader(SoundCloudClient(http))
        media = await downloader.inspect("https://soundcloud.com/artist/track")
        quality = media.qualities[0]
        assert quality.key == "mp3_sq"
        assert quality.protocol == ("progressive" if failure_stage == "selection" else "hls")
        source = await downloader.resolve(media, quality)
    assert source.protocol == "hls"
    assert source.url == "https://cdn.example/full.m3u8"


async def test_preview_url_is_rejected_during_inspection_and_after_menu_selection():
    page, _ = track_page([variant("aac_160k"), variant("aac_256k")])
    expired = False

    def respond(request):
        if request.url.host == "soundcloud.com":
            return httpx.Response(200, text=page)
        preview = expired or request.url.path == "/aac_256k"
        return httpx.Response(
            200,
            json={
                "url": "https://cdn.example/playlist/0/30/preview.m3u8"
                if preview
                else "https://cdn.example/full.m3u8"
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = SoundCloudDownloader(SoundCloudClient(http))
        media = await downloader.inspect("https://soundcloud.com/artist/track")
        assert [q.key for q in media.qualities] == ["aac_160"]
        expired = True
        with pytest.raises(DownloadError):
            await downloader.resolve(media, media.qualities[0])


async def test_api_resolve_recovers_when_html_track_hydration_is_missing():
    page, track = track_page([variant("aac_160k")], include_track=False)

    def respond(request):
        if request.url.host == "soundcloud.com":
            return httpx.Response(200, text=page)
        if request.url.path == "/resolve":
            assert request.url.params["url"] == "https://soundcloud.com/artist/track"
            return httpx.Response(200, json=track)
        return httpx.Response(200, json={"url": "https://cdn.example/full.m3u8"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await SoundCloudDownloader(SoundCloudClient(http)).inspect(
            "https://soundcloud.com/artist/track"
        )
    assert isinstance(media, Media)
    assert media.content_id == "123"


async def test_metadata_resolution_has_bounded_concurrency():
    page, _ = track_page([variant(f"aac_{bitrate}k") for bitrate in range(64, 72)])
    active = peak = 0

    async def respond(request):
        nonlocal active, peak
        if request.url.host == "soundcloud.com":
            return httpx.Response(200, text=page)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={"url": "https://cdn.example/full.m3u8"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await SoundCloudDownloader(SoundCloudClient(http)).inspect(
            "https://soundcloud.com/artist/track"
        )
    assert len(media.qualities) == 8
    assert peak == 4


async def test_unavailable_original_does_not_hide_playable_formats():
    page, track = track_page([variant("aac_160k")])
    track.update(downloadable=True, has_downloads_left=True)

    def respond(request):
        if request.url.host == "soundcloud.com":
            return httpx.Response(200, text=page)
        if request.url.path == "/tracks/123":
            return httpx.Response(200, json=track)
        if request.url.path.endswith("/download"):
            return httpx.Response(200, json={"redirectUri": "https://cdn.example/original.wav"})
        if request.method == "HEAD":
            assert "authorization" not in request.headers
            return httpx.Response(403)
        return httpx.Response(200, json={"url": "https://cdn.example/full.m3u8"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        account = SoundCloudClient(http, "test-token")
        media = await SoundCloudDownloader(SoundCloudClient(http), {"account": account}).inspect(
            "https://soundcloud.com/artist/track"
        )
    assert [quality.key for quality in media.qualities] == ["aac_160"]
