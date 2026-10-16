"""Replay the Music songs schema observed for the reported Radio Edit."""

import asyncio
import copy
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from downloader_bot.downloaders.spotify.client import SpotifyClient
from downloader_bot.downloaders.spotify.downloader import SpotifyDownloader
from downloader_bot.downloaders.spotify.parser import match_recording, music_recordings, read_track
from schemas.media import DownloadError, SiteHTTPError

TRACK = "1gihuPhrLraKYrJMAEONyc"
VIDEO = "Nh_bdvA8cVM"
PAGE = f"https://open.spotify.com/track/{TRACK}"


def metadata():
    entity = {
        "id": TRACK,
        "uri": f"spotify:track:{TRACK}",
        "type": "track",
        "name": "Feel So Close - Radio Edit",
        "artists": [{"name": "Calvin Harris"}],
        "duration": 206413,
    }
    return (
        '<script id="__NEXT_DATA__">'
        + json.dumps({"props": {"pageProps": {"state": {"data": {"entity": entity}}}}})
        + "</script>"
    )


def song(
    *, identity=VIDEO, title="Feel So Close (Radio Edit)", artist="Calvin Harris", seconds="3:28"
):
    def column(runs):
        return {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": runs}}}

    def browse(text, kind):
        return {
            "text": text,
            "navigationEndpoint": {
                "browseEndpoint": {
                    "browseEndpointContextSupportedConfigs": {
                        "browseEndpointContextMusicConfig": {"pageType": kind}
                    }
                }
            },
        }

    return {
        "musicResponsiveListItemRenderer": {
            "playlistItemData": {"videoId": identity},
            "flexColumns": [
                column(
                    [
                        {
                            "text": title,
                            "navigationEndpoint": {
                                "watchEndpoint": {
                                    "videoId": identity,
                                    "watchEndpointMusicSupportedConfigs": {
                                        "watchEndpointMusicConfig": {
                                            "musicVideoType": "MUSIC_VIDEO_TYPE_ATV"
                                        }
                                    },
                                }
                            },
                        }
                    ]
                ),
                column(
                    [
                        browse(artist, "MUSIC_PAGE_TYPE_ARTIST"),
                        {"text": " • "},
                        browse("Feel So Close", "MUSIC_PAGE_TYPE_ALBUM"),
                        {"text": " • "},
                        {"text": seconds},
                    ]
                ),
                column([{"text": "635M plays"}]),
            ],
        }
    }


def catalog(*songs):
    return {
        "contents": {
            "tabbedSearchResultsRenderer": {
                "tabs": [
                    {
                        "tabRenderer": {
                            "content": {
                                "sectionListRenderer": {
                                    "contents": [{"musicShelfRenderer": {"contents": list(songs)}}]
                                }
                            }
                        }
                    }
                ]
            }
        }
    }


def media():
    return read_track(metadata(), TRACK)


def test_reported_general_video_results_fail_while_music_catalog_preserves_a_valid_match():
    # Observed durations: Spotify rounds to 206; radio-edit video hits were 209.
    with pytest.raises(DownloadError, match="نسخه"):
        match_recording(
            {
                "entries": [
                    {
                        "id": "R8egmh5dWzo",
                        "title": "Feel So Close (Radio Edit) - Calvin Harris",
                        "channel": "Jackson George",
                        "duration": 209,
                    }
                ]
            },
            media(),
        )
    assert match_recording(music_recordings(catalog(song())), media()) == (VIDEO, 208)


@pytest.mark.parametrize(
    "change",
    [
        {"artist": "Another Artist"},
        {"title": "Feel So Close (Extended Mix)"},
        {"title": "Feel So Close (Radio Edit) [Karaoke Version]"},
        {"title": "Feel So Close (Radio Edit) Cover"},
        {"seconds": "0:30"},
        {"seconds": "4:07"},
        {"seconds": "3:29"},
        {"seconds": "3:99"},
    ],
)
def test_song_catalog_keeps_artist_version_and_two_second_duration_guards(change):
    with pytest.raises(DownloadError) as error:
        match_recording(music_recordings(catalog(song(**change))), media())
    assert error.value.code == "spotify_no_matching_audio"


@pytest.mark.parametrize(
    "malformation", ["missing_artist", "album_artist", "wrong_id", "video", "shape"]
)
def test_incomplete_or_ambiguous_song_metadata_is_never_inferred_from_the_query(malformation):
    row = song()["musicResponsiveListItemRenderer"]
    runs = row["flexColumns"][1]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"]
    watch = row["flexColumns"][0]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"][0][
        "navigationEndpoint"
    ]["watchEndpoint"]
    if malformation == "missing_artist":
        runs[0].pop("navigationEndpoint")
    elif malformation == "album_artist":
        runs[0]["navigationEndpoint"]["browseEndpoint"]["browseEndpointContextSupportedConfigs"][
            "browseEndpointContextMusicConfig"
        ]["pageType"] = "MUSIC_PAGE_TYPE_ALBUM"
    elif malformation == "wrong_id":
        watch["videoId"] = "dGghkjpNCQ8"
    elif malformation == "video":
        watch["watchEndpointMusicSupportedConfigs"]["watchEndpointMusicConfig"][
            "musicVideoType"
        ] = "MUSIC_VIDEO_TYPE_UGC"
    else:
        row["flexColumns"] = [None]
    assert music_recordings(catalog({"musicResponsiveListItemRenderer": row})) == {"entries": []}


def test_music_parser_caps_candidates_and_ignores_rows_outside_song_shelves():
    assert music_recordings({"other": song()}) == {"entries": []}
    wrong = song(title="Another Song")
    parsed = music_recordings(catalog(*[copy.deepcopy(wrong) for _ in range(10)], song()))
    assert len(parsed["entries"]) == 10
    with pytest.raises(DownloadError):
        match_recording(parsed, media())


async def test_reported_track_resolves_from_music_without_a_video_search_or_shared_provider(
    monkeypatch,
):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    visited = []

    def respond(request):
        visited.append((request.url.host, request.url.path))
        if request.url.host == "open.spotify.com":
            return httpx.Response(200, text=metadata())
        if request.url.host == "music.youtube.com":
            body = json.loads(request.content)
            assert body["query"] == "Calvin Harris Feel So Close - Radio Edit"
            assert body["params"] == "EgWKAQIIAWoKEAoQAxAEEAkQBQ=="
            assert body["context"]["client"]["clientName"] == "WEB_REMIX"
            assert "authorization" not in request.headers and "cookie" not in request.headers
            return httpx.Response(200, json=catalog(song()))
        if request.url.path.endswith("/key"):
            assert request.url.params["link"].endswith(VIDEO)
            return httpx.Response(200, json={"key": "test-key"})
        if request.url.path.endswith("/converter"):
            assert json.loads(request.content)["link"].endswith(VIDEO)
            return httpx.Response(
                200,
                json={
                    "status": "tunnel",
                    "filename": "song.mp3",
                    "url": f"https://files.yt-dl.click/tunnel?vid={VIDEO}",
                },
            )
        return httpx.Response(
            206, content=b"ID3" + b"x" * 1021, headers={"Content-Range": "bytes 0-1023/3300000"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = SpotifyDownloader(SpotifyClient(http, http))
        value = await provider.inspect(PAGE)
        source = await provider.resolve(value, value.qualities[0])
        assert source.duration == 208 and source.size_bytes == 3300000
    assert len(visited) == 5
    spawn.assert_not_awaited()


@pytest.mark.parametrize("failure", ["no_match", "schema", "connection", "http"])
async def test_music_unavailability_keeps_the_existing_bounded_video_search(failure):
    def respond(request):
        if failure == "connection":
            raise httpx.ConnectError("Test connection failure")
        return httpx.Response(
            503 if failure == "http" else 200,
            json={} if failure == "schema" else catalog(song(artist="Other")),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SpotifyClient(http, http)
        client._search_query = AsyncMock(return_value=(VIDEO, 208))
        assert await client.search(media()) == (VIDEO, 208)
        client._search_query.assert_awaited_once()


async def test_music_rate_limit_stops_all_search_routes_and_preserves_cooldown():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SpotifyClient(http, http)
        client._search_query = AsyncMock()
        for _ in range(2):
            with pytest.raises(SiteHTTPError):
                await client.search(media())
        client._search_query.assert_not_awaited()
    assert len(calls) == 1


async def test_music_request_and_video_worker_share_the_original_search_deadline():
    async def respond(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=catalog(song()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SpotifyClient(http, http)
        client.search_timeout = 0.02
        client._search_query = AsyncMock()
        with pytest.raises(TimeoutError):
            await client.search(replace(media(), title="Other Song"))
        client._search_query.assert_not_awaited()
