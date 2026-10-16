"""Spotify identity, conservative matching, isolation, bounded I/O and bot ownership."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.state.menus import MenuStore
from downloader_bot.downloaders.router import DownloaderRouter
from downloader_bot.downloaders.spotify.client import SpotifyClient
from downloader_bot.downloaders.spotify.downloader import SpotifyDownloader
from downloader_bot.downloaders.spotify.handler import handle_spotify_link
from downloader_bot.downloaders.spotify.parser import conversion_url, match_recording, read_track
from downloader_bot.downloaders.spotify.urls import content_id, extract_url, normalize_url
from repositories.metadata import public_media
from schemas.media import DownloadError, SiteHTTPError, Source

TRACK = "0VjIjW4GlUZAMYd2vXMi3b"
PAGE = f"https://open.spotify.com/track/{TRACK}"
VIDEO = "fHI8X4OXluQ"
DOWNLOAD = f"https://files.yt-dl.click/tunnel?vid={VIDEO}&token=private-source"


def page(**changes):
    entity = {
        "id": TRACK,
        "uri": f"spotify:track:{TRACK}",
        "type": "track",
        "name": "Blinding Lights",
        "artists": [{"name": "The Weeknd"}],
        "duration": 200040,
        "audioPreview": {"url": "https://p.scdn.co/preview-30-seconds"},
        "visualIdentity": {"image": [{"url": "https://i.scdn.co/image/cover"}]},
        **changes,
    }
    body = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(body) + "</script>"


def media():
    return read_track(page(), TRACK)


def entry(**changes):
    return {
        "id": VIDEO,
        "title": "The Weeknd - Blinding Lights (Official Audio)",
        "channel": "The Weeknd",
        "duration": 201,
        **changes,
    }


@pytest.mark.parametrize(
    "url",
    [
        PAGE,
        PAGE + "?si=tracking",
        PAGE + "#tracking",
        PAGE.replace("/track/", "/intl-fa/track/"),
        PAGE.replace("/track/", "/embed/track/"),
    ],
)
def test_track_identity_ignores_only_supported_locale_tracking_and_embed_wrappers(url):
    assert normalize_url(url) == PAGE and content_id(url) == TRACK
    assert extract_url("آهنگ: " + url) == url.rstrip(".,;!?)]}»")


@pytest.mark.parametrize(
    "url",
    [
        "https://open.spotify.com.evil.org/track/" + TRACK,
        "https://open.spotify.com@evil.org/track/" + TRACK,
        "https://open.spotify.com:444/track/" + TRACK,
        "https://open.spotify.com/track/short",
        "https://spotify.com/track/" + TRACK,
        "https://127.0.0.1/track/" + TRACK,
        PAGE + "/extra",
    ],
)
def test_input_url_boundaries_reject_lookalikes_credentials_and_wrong_ids(url):
    with pytest.raises(DownloadError):
        normalize_url(url)


@pytest.mark.parametrize("kind", ["album", "playlist", "episode", "show", "artist"])
def test_unsupported_collections_are_recognized_but_not_silently_treated_as_one_track(kind):
    url = PAGE.replace("/track/", f"/{kind}/")
    assert extract_url(url) == url
    with pytest.raises(DownloadError) as error:
        normalize_url(url)
    assert error.value.code == "spotify_unsupported_link"


def test_metadata_uses_original_spotify_identity_and_never_publishes_preview_or_fake_quality():
    value = media()
    assert value.content_id == TRACK and value.duration == 200 and value.artist == "The Weeknd"
    assert len(value.qualities) == 1 and value.qualities[0].bitrate == 128
    assert not value.qualities[0].original and "متناظر" in value.qualities[0].label
    assert "preview" not in json.dumps(public_media(value))
    assert "private-source" not in json.dumps(public_media(value))


@pytest.mark.parametrize(
    "values",
    [
        {"id": "4uLU6hMCjMI75M1A2tKUQC"},
        {"type": "episode"},
        {"uri": "spotify:track:wrong"},
        {"duration": True},
        {"duration": float("nan")},
        {"duration": None},
        {"duration": 0},
        {"artists": None},
        {"artists": []},
        {"name": ""},
    ],
)
def test_wrong_or_incomplete_metadata_fails_closed(values):
    with pytest.raises(DownloadError):
        read_track(page(**values), TRACK)


@pytest.mark.parametrize(
    "changed",
    [
        {"duration": 30},
        {"duration": 263},
        {"is_live": True},
        {"id": "invalid"},
        {"duration": None},
        {"title": "The Weeknd - Blinding Lights Remix"},
        {"title": "The Weeknd - Blinding Lights (Live)"},
        {"title": "The Weeknd - Blinding Lights Cover"},
        {"title": "The Weeknd - Blinding Lights Instrumental"},
        {"title": "The Weeknd - Blinding Lights Slowed"},
        {"title": "Another Song"},
        {"title": "Blinding Lights", "channel": "Wrong Artist"},
    ],
)
def test_no_wrong_artist_short_preview_live_or_altered_recording_is_accepted(changed):
    with pytest.raises(DownloadError) as error:
        match_recording({"entries": [entry(**changed)]}, media())
    assert error.value.code == "spotify_no_matching_audio"


def test_matching_prefers_official_audio_over_cover_channels_and_checks_unicode():
    assert match_recording(
        {"entries": [entry(id="XwxLwG2_Sxk", channel="7clouds", duration=200), entry()]}, media()
    ) == (VIDEO, 201)
    persian = replace(media(), title="تقدیر", artist="شادمهر عقیلی")
    assert (
        match_recording(
            {
                "entries": [
                    entry(title="شادمهر عقیلی - تقدیر", channel="شادمهر عقیلی", duration=200)
                ]
            },
            persian,
        )[0]
        == VIDEO
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://files.yt-dl.click/file?vid=" + VIDEO,
        "https://yt-dl.click.evil.org/?vid=" + VIDEO,
        "https://127.0.0.1/?vid=" + VIDEO,
        "https://files.yt-dl.click/?vid=other",
        "https://files.yt-dl.click:444/?vid=" + VIDEO,
        "https://user:password@files.yt-dl.click/?vid=" + VIDEO,
    ],
)
def test_converter_destination_must_match_the_exact_selected_video(url):
    with pytest.raises(DownloadError):
        conversion_url({"status": "tunnel", "filename": "song.mp3", "url": url}, VIDEO)


async def test_full_provider_flow_owns_requests_and_probe_does_not_follow_external_redirects():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "open.spotify.com":
            assert "origin" not in request.headers
            return httpx.Response(200, text=page())
        if request.url.path.endswith("/key"):
            assert request.url.params["link"].endswith(VIDEO)
            return httpx.Response(200, json={"key": "ephemeral-key"})
        if request.url.path.endswith("/converter"):
            assert request.headers["key"] == "ephemeral-key"
            assert json.loads(request.content)["audioBitrate"] == "128"
            return httpx.Response(
                200, json={"status": "tunnel", "filename": "song.mp3", "url": DOWNLOAD}
            )
        assert request.url.host == "files.yt-dl.click" and "key" not in request.headers
        assert request.headers["Range"] == "bytes=0-1023"
        return httpx.Response(
            206, content=b"ID3" + b"x" * 1021, headers={"Content-Range": "bytes 0-1023/3252222"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SpotifyClient(http, http)
        client.search = AsyncMock(return_value=(VIDEO, 203))
        provider = SpotifyDownloader(client)
        selected = await provider.inspect(PAGE)
        source = await provider.resolve(selected, selected.qualities[0])
        assert source.size_bytes == 3252222 and source.duration == 203 and source.url == DOWNLOAD
        assert len(requests) == 4 and client.search.await_count == 1
        assert await provider.resolve(selected, selected.qualities[0]) is source
        assert len(requests) == 4


async def test_short_link_cannot_redirect_to_unrelated_or_private_origins():
    visited = []

    def respond(request):
        visited.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = SpotifyDownloader(SpotifyClient(http, http))
        with pytest.raises(DownloadError):
            await provider.inspect("https://spotify.link/AbCdEf12")
    assert visited == ["spotify.link"]


async def test_thousand_same_track_inspections_and_resolutions_share_one_source():
    source = Source(DOWNLOAD, "progressive", duration=203)
    client = SimpleNamespace(
        timeout=8,
        search_timeout=20,
        track=AsyncMock(return_value=media()),
        audio=AsyncMock(return_value=source),
    )
    provider = SpotifyDownloader(client)
    result = await asyncio.gather(*(provider.inspect(PAGE) for _ in range(1000)))
    assert client.track.await_count == 1
    output = await asyncio.gather(
        *(provider.resolve(value, value.qualities[0]) for value in result)
    )
    assert all(value is source for value in output) and client.audio.await_count == 1
    assert provider._locks.count == 0


async def test_rate_limit_cooldown_stops_network_retries():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SpotifyClient(http, http)
        for _ in range(5):
            with pytest.raises(SiteHTTPError):
                await client.track(PAGE)
    assert len(requests) == 1


@pytest.mark.parametrize("response", [b"<html>Error</html>", b"{}", b"[]"])
async def test_non_audio_converter_results_cannot_become_download_sources(response):
    def respond(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"key": "key"})
        if request.url.path.endswith("/converter"):
            return httpx.Response(
                200, json={"status": "tunnel", "filename": "song.mp3", "url": DOWNLOAD}
            )
        return httpx.Response(200, content=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SpotifyClient(http, http)
        client.search = AsyncMock(return_value=(VIDEO, 203))
        with pytest.raises(DownloadError) as error:
            await client.audio(media())
        assert error.value.code == "spotify_invalid_audio"


async def test_handler_routes_to_spotify_and_creates_sender_owned_group_quality_buttons():
    provider = SimpleNamespace(inspect=AsyncMock(return_value=media()), catalog_key=content_id)
    router = DownloaderRouter({"spotify": provider})
    assert router.site_for(PAGE) == "spotify" and router.catalog_key(PAGE) == "spotify:" + TRACK
    menus = MenuStore()
    event = SimpleNamespace(
        raw_text=PAGE,
        sender_id=7,
        chat_id=-1007,
        id=42,
        respond=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
    )
    await handle_spotify_link(event, router, menus)
    options = event.respond.return_value.edit.call_args.kwargs
    prefix, token, index = options["buttons"][0][0].type.data.decode().split(":")
    assert prefix == "sp" and index == "0"
    assert menus.get(token, 7, -1007).message_id == 42
    with pytest.raises(DownloadError):
        menus.get(token, 8, -1007)


@pytest.mark.parametrize("operation", ["metadata", "source"])
async def test_thousand_failures_share_cooldown_instead_of_repeating_upstream(operation):
    client = SimpleNamespace(
        timeout=8,
        search_timeout=20,
        track=AsyncMock(return_value=media()),
        audio=AsyncMock(return_value=None),
    )
    provider = SpotifyDownloader(client)
    failure = DownloadError("Temporarily unavailable", code="spotify_test_unavailable")
    if operation == "metadata":
        client.track.side_effect = failure

        async def run():
            return await provider.inspect(PAGE)
    else:
        client.audio.side_effect = failure

        async def run():
            value = media()
            return await provider.resolve(value, value.qualities[0])

    results = await asyncio.gather(*(run() for _ in range(1000)), return_exceptions=True)
    assert all(isinstance(row, DownloadError) and row.code == failure.code for row in results)
    assert (client.track if operation == "metadata" else client.audio).await_count == 1
    assert provider._locks.count == 0


async def test_expired_sources_refresh_and_restored_public_metadata_remains_resolvable():
    from repositories.postgres.workflow import read_media

    client = SimpleNamespace(
        timeout=8,
        search_timeout=20,
        track=AsyncMock(return_value=media()),
        audio=AsyncMock(return_value=Source(DOWNLOAD, "progressive", duration=201)),
    )
    provider = SpotifyDownloader(client, source_ttl=0)
    restored = read_media(public_media(media()))
    await provider.resolve(restored, restored.qualities[0])
    await provider.resolve(restored, restored.qualities[0])
    assert client.track.await_count == 1 and client.audio.await_count == 2


def subprocess_result(payload, *, returncode=0, stderr=b""):
    stdout_reader, stderr_reader = asyncio.StreamReader(), asyncio.StreamReader()
    stdout_reader.feed_data(payload)
    stdout_reader.feed_eof()
    stderr_reader.feed_data(stderr)
    stderr_reader.feed_eof()
    process = SimpleNamespace(
        stdout=stdout_reader,
        stderr=stderr_reader,
        returncode=returncode,
        wait=AsyncMock(return_value=returncode),
        communicate=AsyncMock(return_value=(b"", b"")),
    )
    process.kill = lambda: setattr(process, "returncode", -9)
    return process


async def test_actual_search_worker_arguments_are_bounded_and_do_not_use_other_provider_credentials(
    monkeypatch,
):
    process = subprocess_result(json.dumps({"entries": [entry()]}).encode())
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    client = SpotifyClient(None, None)
    assert await client.search(media()) == (VIDEO, 201)
    args = spawn.call_args.args
    assert "--ignore-config" in args and "--no-plugin-dirs" in args
    assert "--cookies" not in args and args[-2] == "--" and args[-1].startswith("ytsearch10:")
    process.communicate.assert_awaited_once()


async def test_search_response_limit_kills_and_reaps_worker(monkeypatch):
    process = subprocess_result(b"x" * (513 * 1024), returncode=None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(DownloadError):
        await SpotifyClient(None, None).search(media())
    assert process.returncode == -9
    process.communicate.assert_awaited_once()


async def test_cancelled_search_kills_and_reaps_worker(monkeypatch):
    process = subprocess_result(b"", returncode=None)
    process.stdout = asyncio.StreamReader()
    started = asyncio.Event()

    async def spawn(*args, **options):
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(SpotifyClient(None, None).search(media()))
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9
    process.communicate.assert_awaited_once()


async def test_search_rate_limit_is_preserved_and_prevents_next_worker(monkeypatch):
    process = subprocess_result(b"", returncode=1, stderr=b"HTTP Error 429")
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    client = SpotifyClient(None, None)
    for _ in range(2):
        with pytest.raises(SiteHTTPError):
            await client.search(media())
    spawn.assert_awaited_once()
