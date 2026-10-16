"""Real FFmpeg rejects local-file sources and forged raw HTTP headers."""

import asyncio

import imageio_ffmpeg
import pytest

from bot.transfers.streaming import remux_arguments
from schemas.media import DownloadError, Quality, Source

QUALITY = Quality("mp3", "MP3", "mp3", 128, "mp3", "audio/mpeg", "progressive", "")


async def test_remote_media_input_cannot_open_local_files(tmp_path):
    path = tmp_path / "private.mp3"
    path.write_bytes(b"ID3PRIVATE_LOCAL_CANARY")
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(),
        *remux_arguments(Source(path.as_uri(), "hls"), QUALITY),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, errors = await asyncio.wait_for(process.communicate(), 5)
    assert process.returncode != 0 and not output
    assert b"not on whitelist" in errors


@pytest.mark.parametrize(
    "headers",
    [
        {"Referer": "https://example.com/\r\nHost: private"},
        {"X-Test\r\nHost": "private"},
        {"Authorization": "value\x00"},
    ],
)
def test_source_headers_cannot_inject_other_headers_or_requests(headers):
    with pytest.raises(DownloadError) as error:
        remux_arguments(Source("https://cdn.example/audio", "hls", headers=headers), QUALITY)
    assert error.value.code == "invalid_source_headers"
