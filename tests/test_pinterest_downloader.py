import asyncio
import json
import struct
from dataclasses import replace

import httpx
import pytest

from downloader_bot.downloaders.pinterest.client import PinterestClient
from downloader_bot.downloaders.pinterest.downloader import PinterestDownloader
from downloader_bot.downloaders.pinterest.parser import (
    cdn_url,
    mp4_metadata,
    pin_id,
    read_page_pin,
    read_pin,
)
from downloader_bot.models import DownloadError
from downloader_bot.urls import extract_pinterest_url

PIN = "123456789"
PAGE = f"https://www.pinterest.com/pin/{PIN}/"
CDN = "https://v1.pinimg.com/videos/demo/"
PLAYLIST = "#EXTM3U\n#EXTINF:12,\nsegment.ts\n#EXT-X-ENDLIST\n"
MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=720x1280,CODECS="avc1.64001f,mp4a.40.2",AUDIO="sound"
720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=360x640,CODECS="avc1.64001e,mp4a.40.2",AUDIO="sound"
360.m3u8
"""


def box(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def mp4(width=720, height=1280, codec=b"avc1"):
    mvhd = bytearray(100)
    struct.pack_into(">II", mvhd, 12, 1000, 12000)
    tkhd = bytearray(84)
    struct.pack_into(">II", tkhd, 76, width << 16, height << 16)
    stsd = b"\0" * 4 + struct.pack(">I", 1) + box(codec, b"\0" * 78)
    mdia = box(b"hdlr", b"\0" * 8 + b"vide") + box(b"minf", box(b"stbl", box(b"stsd", stsd)))
    moov = box(b"mvhd", mvhd) + box(b"trak", box(b"tkhd", tkhd) + box(b"mdia", mdia))
    return box(b"ftyp", b"isom" + b"\0" * 4) + box(b"moov", moov)


def pin_data(*, video=True):
    data = {
        "id": PIN,
        "title": "Demo",
        "pinner": {"full_name": "Author"},
        "images": {
            "orig": {
                "url": "https://i.pinimg.com/originals/demo.jpg",
                "width": 1080,
                "height": 1920,
            },
            "474x": {"url": "https://i.pinimg.com/474x/demo.jpg", "width": 474, "height": 842},
            "60x60": {"url": "https://i.pinimg.com/60x60/demo.jpg", "width": 60, "height": 60},
        },
    }
    if video:
        data["videos"] = {
            "id": "video",
            "video_list": {
                "V_720P": {
                    "url": CDN + "video.mp4?token=old",
                    "width": 1080,
                    "height": 1920,
                    "duration": 12000,
                },
                "V_HLSV4": {
                    "url": CDN + "master.m3u8",
                    "width": 1080,
                    "height": 1920,
                    "duration": 12000,
                },
                "V_HLSV3_MOBILE": {"url": CDN + "master.m3u8", "duration": 12000},
            },
        }
    return data


def api(data):
    return httpx.Response(200, json={"resource_response": {"data": data}})


@pytest.mark.parametrize(
    "text",
    [
        "pinterest.com/pin/123456789/",
        "لینک www.pinterest.com/pin/123456789/",
        "(https://pin.it/abcDEF1).",
        "http://www.pinterest.co.uk/pin/123456789/",
        "https://co.pinterest.com/pin/123456789/",
        "HTTPS://PINTEREST.COM/pin/123456789/",
    ],
)
def test_links_are_normalized(text):
    assert extract_pinterest_url(text).lower().startswith("https://")


@pytest.mark.parametrize(
    "text",
    [
        "https://pinterest.com.evil.org/pin/1",
        "https://notpinterest.com/pin/1",
        "https://evil.org/pinterest.com/pin/1",
        "me@pinterest.com/pin/1",
        "https://pinterest.com@evil.org/pin/1",
        "https://evil.pinterest.com/pin/1",
        "https://pin.it.evil.org/abc",
        "ftp://pinterest.com/pin/1",
    ],
)
def test_lookalikes_are_rejected(text):
    assert extract_pinterest_url(text) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://i.pinimg.com.evil.org/file.mp4",
        "http://i.pinimg.com/file.jpg",
        "https://user:password@i.pinimg.com/file.jpg",
        "https://127.0.0.1/file.mp4",
        "https://i.pinimg.com:123/file.jpg",
        "file:///secret.jpg",
    ],
)
def test_only_pinterest_cdn_urls_are_accepted(url):
    assert cdn_url(url) is None


def test_slugged_pin_identity_is_stable_and_boards_are_rejected():
    assert pin_id("https://www.pinterest.co.uk/pin/my-video--123456789/sent/?x=1") == PIN
    with pytest.raises(DownloadError):
        pin_id("https://www.pinterest.com/user/board/")


def test_images_exclude_crops_and_use_only_published_originals():
    data = pin_data(video=False)
    media = read_pin(data, PIN)
    assert len(media.qualities) == 2 and media.qualities[0].original
    del data["images"]["orig"]
    assert len(read_pin(data, PIN).qualities) == 1
    assert not read_pin(data, PIN).qualities[0].original


def test_video_and_story_do_not_silently_become_the_poster():
    data = pin_data(video=False)
    data["is_video"] = True
    with pytest.raises(DownloadError):
        read_pin(data, PIN)
    data["story_pin_data"] = {
        "pages": [{"id": "page", "blocks": [{"id": "block", "video": {"video_list": {}}}]}]
    }
    with pytest.raises(DownloadError):
        read_pin(data, PIN)


def test_carousel_items_have_distinct_cache_keys():
    data = pin_data(video=False)
    data["carousel_data"] = {
        "carousel_slots": [
            {"id": "first", "images": data["images"]},
            {"id": "second", "images": data["images"]},
        ]
    }
    media = read_pin(data, PIN)
    assert len({quality.key for quality in media.qualities}) == 4
    assert any("رسانه 2" in quality.label for quality in media.qualities)


def test_legacy_page_matches_exact_id_and_ignores_related_pins():
    right = pin_data(video=False)
    wrong = {**right, "id": "999"}
    page = '<script id="__PWS_DATA__">' + json.dumps({"pins": [right, wrong]}) + "</script>"
    assert read_page_pin(page, PIN)["id"] == PIN
    with pytest.raises(DownloadError):
        read_page_pin(page, "777")


def relay_page():
    return (
        '<script data-relay-completed-request="true">window.__PWS_RELAY_REGISTER_COMPLETED_REQUEST__("query", '
        + json.dumps(
            {
                "data": {
                    "pin": {
                        "entityId": PIN,
                        "id": "base64-pin",
                        "title": "Demo",
                        "images_orig": {
                            "url": "https://i.pinimg.com/originals/demo.jpg",
                            "width": 1080,
                            "height": 1920,
                        },
                        "videos": {
                            "entityId": "video",
                            "videoList": {"v720P": pin_data()["videos"]["video_list"]["V_720P"]},
                            "videoUrls": [CDN + "small.mp4"],
                        },
                    }
                }
            }
        )
        + ");</script>"
    )


def test_relay_json_exposes_published_extra_urls_without_executing_javascript():
    data = read_page_pin(relay_page(), PIN)
    assert data["videos"]["id"] == "video"
    media = read_pin(data, PIN)
    assert {quality.endpoint for quality in media.qualities} == {
        CDN + "video.mp4?token=old",
        CDN + "small.mp4",
    }
    with pytest.raises(DownloadError):
        read_page_pin(
            '<script data-relay-completed-request>window.__PWS_RELAY_REGISTER_COMPLETED_REQUEST__("q", alert(1));</script>',
            PIN,
        )


def test_mp4_boxes_report_actual_dimensions_codec_and_duration():
    probe = mp4_metadata(mp4(540, 960, b"av01"))
    assert (probe.width, probe.height, probe.duration, probe.codec) == (540, 960, 12, "av1")
    assert not mp4_metadata(b"<html>error</html>").valid
    assert mp4_metadata(b"padding" + mp4()[16:], tail=True).width == 720


async def test_selection_refreshes_url_and_preserves_actual_resolution_and_audio():
    calls = 0
    visits = []

    def respond(request):
        nonlocal calls
        visits.append(str(request.url))
        if request.url.path.startswith("/resource/"):
            calls += 1
            data = pin_data()
            data["videos"]["video_list"]["V_720P"]["url"] = CDN + f"video.mp4?token={calls}"
            return api(data)
        if request.url.host == "www.pinterest.com":
            return httpx.Response(200, text="<html></html>")
        if request.url.path.endswith(".mp4"):
            return httpx.Response(206, content=mp4())
        if request.url.path.endswith("master.m3u8"):
            return httpx.Response(200, text=MASTER)
        if request.url.path.endswith("360.m3u8"):
            return httpx.Response(404)
        return httpx.Response(200, text=PLAYLIST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = PinterestDownloader(PinterestClient(http))
        media = await downloader.inspect(PAGE)
        assert len(media.qualities) == 2
        direct, hls = media.qualities
        assert (direct.width, direct.height) == (720, 1280)
        visits.clear()
        assert (await downloader.resolve(media, direct)).url.endswith("token=2")
        assert not any(".m3u8" in url for url in visits)
        visits.clear()
        assert (await downloader.resolve(media, hls)).audio_url == CDN + "audio.m3u8"
        assert not any("360.m3u8" in url or ".mp4" in url for url in visits)


async def test_extra_mp4_qualities_are_available_for_direct_telegram_fetch():
    def respond(request):
        if request.url.path.startswith("/resource/"):
            data = pin_data()
            data["videos"]["video_list"] = {"V_720P": data["videos"]["video_list"]["V_720P"]}
            return api(data)
        if request.url.host == "www.pinterest.com":
            return httpx.Response(200, text=relay_page())
        return httpx.Response(
            206, content=mp4(360, 640, b"av01") if request.url.path.endswith("small.mp4") else mp4()
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = PinterestDownloader(PinterestClient(http))
        media = await downloader.inspect(PAGE)
        assert len(media.qualities) == 2
        small = next(quality for quality in media.qualities if quality.width == 360)
        assert small.protocol == "progressive" and small.codec == "av1"
        assert (await downloader.resolve(media, small)).url == CDN + "small.mp4"


async def test_redirects_cannot_escape_pinterest():
    visits = []

    def respond(request):
        visits.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example/pin/123456789/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = PinterestClient(http)
        with pytest.raises(DownloadError):
            await client.identify("https://pin.it/abc")
        with pytest.raises(DownloadError):
            await client.probe_media(CDN + "video.mp4", "video/mp4")
    assert visits == ["api.pinterest.com", "v1.pinimg.com"]


async def test_shortener_identifies_same_pin_without_fetching_landing_page():
    def respond(request):
        assert request.url.host == "api.pinterest.com"
        return httpx.Response(302, headers={"location": PAGE + "sent/?invite_code=example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await PinterestClient(http).identify("https://pin.it/abc") == PIN


async def test_ignored_range_is_closed_and_cancellation_is_preserved():
    class Body(httpx.AsyncByteStream):
        consumed = 0
        closed = False

        async def __aiter__(self):
            for _ in range(1000):
                self.consumed += 1
                yield b"GIF89a" + b"x" * 1018

        async def aclose(self):
            self.closed = True

    body = Body()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
    ) as http:
        assert (
            await PinterestClient(http).probe_media("https://i.pinimg.com/a.gif", "image/gif")
        ).valid
    assert body.consumed == 1 and body.closed

    async def cancel(_):
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(cancel)) as http:
        with pytest.raises(asyncio.CancelledError):
            await PinterestDownloader(PinterestClient(http)).inspect(PAGE)


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text.replace("#EXT-X-ENDLIST", ""),
        lambda text: text.replace("segment.ts", "https://evil.example/segment.ts"),
        lambda text: text.replace("segment.ts", "file:///secret"),
        lambda text: text.replace("#EXTINF:12,", "#EXTINF:1,"),
        lambda text: text.replace(
            "#EXTINF:12,", '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"\n#EXTINF:12,'
        ),
    ],
)
async def test_invalid_partial_and_offsite_hls_is_not_offered(change):
    def respond(request):
        if request.url.path.startswith("/resource/"):
            data = pin_data()
            data["videos"]["video_list"] = {"V_HLSV4": data["videos"]["video_list"]["V_HLSV4"]}
            return api(data)
        if request.url.host == "www.pinterest.com":
            return httpx.Response(200, text="")
        return httpx.Response(200, text=change(PLAYLIST))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError):
            await PinterestDownloader(PinterestClient(http)).inspect(PAGE)


async def test_invalid_selection_is_rejected_before_any_request():
    media = read_pin(pin_data(video=False), PIN)

    def reject(_):
        pytest.fail("No request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        downloader = PinterestDownloader(PinterestClient(http))
        with pytest.raises(DownloadError):
            await downloader.resolve(replace(media, site="soundcloud"), media.qualities[0])
        with pytest.raises(DownloadError):
            await downloader.resolve(
                media, replace(media.qualities[0], endpoint="https://evil.example/")
            )


async def test_rate_limit_is_actionable_and_does_not_trigger_extra_requests():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DownloadError, match="محدودیت"):
            await PinterestDownloader(PinterestClient(http)).inspect(PAGE)
    assert len(calls) == 1


async def test_a_published_extra_encode_survives_a_relay_response_that_omits_it():
    signature = "a" * 32
    original = CDN + signature + ".mp4"
    extra = CDN + signature + "_360w.mp4"

    def respond(request):
        if request.url.path.startswith("/resource/"):
            data = pin_data()
            data["videos"]["video_list"] = {"V_720P": {"url": original, "duration": 12000}}
            return api(data)
        if request.url.host == "www.pinterest.com":
            return httpx.Response(200, text="")
        return httpx.Response(206, content=mp4(360, 640, b"av01"))

    data = pin_data()
    data["videos"]["video_list"] = {"CDN_extra": {"url": extra, "duration": 12000}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = PinterestDownloader(PinterestClient(http))
        media = read_pin(data, PIN)
        formats = await downloader._formats(media.qualities)
        media = replace(media, qualities=tuple(quality for quality, _ in formats))
        assert (await downloader.resolve(media, media.qualities[0])).url == extra


async def test_one_hls_rendition_network_failure_preserves_other_verified_renditions():
    def respond(request):
        if request.url.path.startswith("/resource/"):
            data = pin_data()
            data["videos"]["video_list"] = {"V_HLSV4": data["videos"]["video_list"]["V_HLSV4"]}
            return api(data)
        if request.url.host == "www.pinterest.com":
            return httpx.Response(200, text="")
        if request.url.path.endswith("master.m3u8"):
            return httpx.Response(200, text=MASTER)
        if request.url.path.endswith("360.m3u8"):
            raise httpx.ConnectTimeout("temporary network failure")
        return httpx.Response(200, text=PLAYLIST)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        media = await PinterestDownloader(PinterestClient(http)).inspect(PAGE)
        assert len(media.qualities) == 1 and media.qualities[0].width == 720


@pytest.mark.parametrize("failure", ["timeout", "401", "503"])
async def test_failed_pin_api_falls_back_to_verified_public_page_media(failure):
    def respond(request):
        if request.url.path.startswith("/resource/"):
            if failure == "timeout":
                raise httpx.ConnectTimeout("temporary API failure")
            return httpx.Response(int(failure))
        if request.url.host == "www.pinterest.com":
            return httpx.Response(200, text=relay_page())
        return httpx.Response(206, content=mp4())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = PinterestDownloader(PinterestClient(http))
        media = await downloader.inspect(PAGE)
        assert media.content_id == PIN and media.qualities
        assert (await downloader.resolve(media, media.qualities[0])).url.startswith(CDN)
