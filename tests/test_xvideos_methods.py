import asyncio
import struct

import httpx
import pytest

from downloader_bot.downloaders.xvideos.client import XVideosClient
from downloader_bot.downloaders.xvideos.downloader import XVideosDownloader
from downloader_bot.downloaders.xvideos.parser import extract_links, mp4_dimensions
from downloader_bot.schemas.media import DownloadError

PAGE = "https://www.xvideos.com/video.demo/_"
FTYP = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"


def box(kind, payload, *, extended=False):
    if extended:
        return struct.pack(">I4sQ", 1, kind, len(payload) + 16) + payload
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def track(width=640, height=360, *, handler=b"vide", version=0, matrix=None):
    length = 96 if version == 1 else 84
    value = bytearray(length)
    value[0] = version
    value[3] = 3
    value[-44:-8] = struct.pack(">9I", *(matrix or (65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824)))
    value[-8:] = struct.pack(">II", width << 16, height << 16)
    return box(
        b"trak", box(b"tkhd", value) + box(b"mdia", box(b"hdlr", b"\0" * 8 + handler + b"\0" * 12))
    )


@pytest.mark.parametrize("version,extended", [(0, False), (1, False), (0, True), (1, True)])
def test_mp4_track_dimensions_ignore_audio_and_need_no_full_download(version, extended):
    data = FTYP + box(
        b"moov", track(1, 1, handler=b"soun") + track(version=version), extended=extended
    )
    assert mp4_dimensions(data) == (640, 360)


def test_partial_moov_keeps_complete_video_header_without_reading_sample_tables():
    data = FTYP + box(b"moov", track() + box(b"free", b"\0" * 4096))
    assert mp4_dimensions(data[:1024]) == (640, 360)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        FTYP,
        FTYP + box(b"moov", track())[:80],
        FTYP + box(b"mdat", box(b"moov", track())),
        FTYP + box(b"moov", track(handler=b"soun")),
        FTYP + box(b"moov", track(width=0)),
        FTYP + box(b"moov", track(width=20000)),
        FTYP + box(b"moov", track(version=2)),
        FTYP + box(b"moov", track(matrix=(0, 65536, 0, 4294901760, 0, 0, 0, 0, 1073741824))),
        FTYP + box(b"moov", b"\0\0\0\x04trak" + track()),
        FTYP + box(b"moov", b"\0\0\0\x01trak\0"),
    ],
)
def test_missing_invalid_or_transformed_dimensions_remain_unknown(data):
    assert mp4_dimensions(data) is None


def test_percent_encoded_legacy_mp4_precedes_unrelated_literal_ad():
    info = extract_links(
        '<param value="flv_url=https%3A%2F%2Fcdn.example%2Factual.mp4%3Ftoken%3Done%26a%3D2&amp;id=1">'
        '<script>const ad="https://ads.example/ad.mp4";</script>',
        PAGE,
    )
    assert info["high"] == "https://cdn.example/actual.mp4?token=one&a=2"


def test_structured_video_source_has_priority_over_legacy_flash_parameter():
    info = extract_links(
        '<source src="https://cdn.example/video.mp4">flv_url=https%3A%2F%2Fcdn.example%2Fold.mp4&',
        PAGE,
    )
    assert info["high"] == "https://cdn.example/video.mp4"


async def test_actual_mp4_pixels_reach_menu_and_changed_pixels_cannot_replace_quality():
    dimensions = (640, 360)
    samples = []

    def respond(request):
        if request.url.host == "www.xvideos.com":
            return httpx.Response(200, text="setVideoUrlHigh('https://cdn.example/video_720p.mp4')")
        samples.append(request.headers["range"])
        return httpx.Response(200, content=FTYP + box(b"moov", track(*dimensions)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        downloader = XVideosDownloader(XVideosClient(http))
        media = await downloader.inspect(PAGE)
        quality = media.qualities[0]
        assert (quality.width, quality.height) == (640, 360)
        assert "640×360" in quality.label and quality.key == "high_640x360"
        assert (await downloader.resolve(media, quality)).protocol == "progressive"
        dimensions = (444, 250)
        with pytest.raises(DownloadError, match="کیفیت انتخاب‌شده"):
            await downloader.resolve(media, quality)
        fresh = await downloader.inspect(PAGE)
        assert fresh.content_id == media.content_id
        assert fresh.qualities[0].key == "high_444x250"
        assert fresh.qualities[0].key != quality.key
    assert samples == ["bytes=0-1023"] * 4


async def test_bounded_metadata_matches_real_ffmpeg_mp4():
    import imageio_ffmpeg

    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=size=96x64:rate=1",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-movflags",
        "frag_keyframe+empty_moov",
        "-f",
        "mp4",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        data, diagnostic = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == 0, diagnostic.decode(errors="replace")
        assert mp4_dimensions(data[:1024]) == (96, 64)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
