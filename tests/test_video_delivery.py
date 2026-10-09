import asyncio
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from telethon import errors, types

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.progress import TransferProgress
from downloader_bot.bot.transfers.streaming import media_chunks
from downloader_bot.schemas.media import DownloadError, Media, Quality, Source

QUALITY = Quality("hls", "HLS", "video", None, "mp4", "video/mp4", "hls", "endpoint")


@pytest.mark.parametrize(
    "duration, actual, complete", [(4, 2, False), (4, 3.8, True), (20, 19.3, True)]
)
async def test_short_hls_truncation_is_rejected_without_rejecting_rounded_duration(
    monkeypatch, duration, actual, complete
):
    output, diagnostic = asyncio.StreamReader(), asyncio.StreamReader()
    output.feed_data(b"media")
    output.feed_eof()
    diagnostic.feed_data(f"out_time_us={int(actual * 1_000_000)}\n".encode())
    diagnostic.feed_eof()

    async def wait():
        return 0

    async def start(*args, **kwargs):
        return SimpleNamespace(stdout=output, stderr=diagnostic, wait=wait, returncode=0)

    monkeypatch.setattr(
        "downloader_bot.bot.transfers.streaming.asyncio.create_subprocess_exec", start
    )
    progress = TransferProgress()
    async with httpx.AsyncClient() as http:

        async def consume():
            return b"".join(
                [
                    part
                    async for part in media_chunks(
                        http,
                        Source("https://cdn.example/index.m3u8", "hls", duration=duration),
                        QUALITY,
                        "ffmpeg",
                        progress,
                    )
                ]
            )

        if complete:
            assert await consume() == b"media" and progress.download_done
        else:
            with pytest.raises(DownloadError) as failure:
                await consume()
            assert failure.value.code == "media_stream_incomplete" and not progress.download_done


@pytest.mark.parametrize("separate_audio", [False, True])
@pytest.mark.parametrize("single_file", [False, True])
async def test_http_fragmented_hls_downloads_all_segments_and_keeps_audio(
    tmp_path, separate_audio, single_file
):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    manifest = tmp_path / "index.m3u8"
    audio = tmp_path / "audio.m4a"
    create = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=32x32:r=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-map",
        "0:v:0",
        *([] if separate_audio else ["-map", "1:a:0"]),
        "-t",
        "4",
        "-c:v",
        "libx264",
        "-g",
        "10",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "0",
        "-hls_segment_type",
        "fmp4",
        *(["-hls_flags", "single_file"] if single_file else []),
        str(manifest),
        *(["-map", "1:a:0", "-t", "4", "-c:a", "aac", str(audio)] if separate_audio else []),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=tmp_path,
    )
    _, diagnostic = await create.communicate()
    assert create.returncode == 0, diagnostic.decode(errors="replace")
    requested = []
    ranges = []

    async def respond(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            name = urlsplit(request.split(b" ")[1].decode()).path.lstrip("/")
            requested.append(name)
            ranges.append(b"range:" in request.lower())
            body = (tmp_path / name).read_bytes()
            status = "200 OK"
            content_range = ""
            if match := re.search(rb"(?im)^range: bytes=(\d+)-(\d*)\r?$", request):
                start = int(match[1])
                end = min(int(match[2]) if match[2] else len(body) - 1, len(body) - 1)
                content_range = f"Content-Range: bytes {start}-{end}/{len(body)}\r\n"
                body = body[start : end + 1]
                status = "206 Partial Content"
            writer.write(
                f"HTTP/1.1 {status}\r\n{content_range}Content-Length: {len(body)}\r\nAccept-Ranges: bytes\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        source = Source(
            base + manifest.name,
            "hls",
            audio_url=str(audio) if separate_audio else None,
            audio_protocol="progressive" if separate_audio else None,
            duration=4,
            require_audio=True,
        )
        progress = TransferProgress()
        async with httpx.AsyncClient() as http:
            data = b"".join(
                [part async for part in media_chunks(http, source, QUALITY, ffmpeg, progress)]
            )
    assert progress.download_done and progress.seconds >= 3.8
    if single_file:
        assert requested.count("index.m4s") >= 4
        assert any(ranged for name, ranged in zip(requested, ranges) if name == "index.m4s")
    else:
        assert len(set(name for name in requested if name.endswith(".m4s"))) == 4
        assert not any(
            ranged for name, ranged in zip(requested, ranges) if not name.endswith(".m3u8")
        )
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


@pytest.mark.parametrize(
    "extension,video_codec,audio_codec",
    [
        ("mp4", "libx264", "aac"),
        ("webm", "libvpx-vp9", "libopus"),
    ],
)
async def test_real_separate_streams_merge_without_losing_audio(
    tmp_path,
    extension,
    video_codec,
    audio_codec,
):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    video, audio = tmp_path / f"video.{extension}", tmp_path / f"audio.{extension}"
    create = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=32x32:r=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-map",
        "0:v:0",
        "-t",
        "0.5",
        "-c:v",
        video_codec,
        "-pix_fmt",
        "yuv420p",
        str(video),
        "-map",
        "1:a:0",
        "-t",
        "0.5",
        "-c:a",
        audio_codec,
        str(audio),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await create.communicate()
    assert create.returncode == 0, diagnostic.decode(errors="replace")
    quality = Quality(
        "dash", "Dash", video_codec, None, extension, f"video/{extension}", "dash", ""
    )
    async with httpx.AsyncClient() as http:
        chunks = media_chunks(
            http, Source(str(video), "dash", audio_url=str(audio)), quality, ffmpeg
        )
        data = b"".join([chunk async for chunk in chunks])
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
    # A missing audio input must fail rather than deliver a silent video.
    async with httpx.AsyncClient() as http:
        with pytest.raises(DownloadError):
            _ = [
                chunk
                async for chunk in media_chunks(
                    http,
                    Source(str(video), "dash", audio_url=str(tmp_path / "missing.m4a")),
                    quality,
                    ffmpeg,
                )
            ]


@pytest.mark.parametrize("audio_codec", ["aac", "libmp3lame", None])
async def test_real_hls_remux_keeps_picture_and_available_audio(tmp_path, audio_codec):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    manifest = tmp_path / "index.m3u8"
    create = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=32x32:r=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "0.5",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        *(["-c:a", audio_codec] if audio_codec else ["-an"]),
        "-f",
        "hls",
        "-hls_time",
        "1",
        str(manifest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await create.communicate()
    assert create.returncode == 0, diagnostic.decode(errors="replace")
    async with httpx.AsyncClient() as http:
        chunks = media_chunks(http, Source(str(manifest), "hls"), QUALITY, ffmpeg)
        data = b"".join([chunk async for chunk in chunks])
    assert b"ftyp" in data[:32]
    # Decoding with both required maps fails if the remux drops either track.
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
        *(["-map", "0:a:0"] if audio_codec else []),
        "-f",
        "null",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await verify.communicate(data)
    assert verify.returncode == 0, diagnostic.decode(errors="replace")


async def test_separate_hls_audio_is_included_in_selected_video(tmp_path):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    video, audio = tmp_path / "video.m3u8", tmp_path / "audio.m3u8"
    create = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=32x32:r=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-map",
        "0:v:0",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "hls",
        str(video),
        "-map",
        "1:a:0",
        "-t",
        "1",
        "-c:a",
        "aac",
        "-f",
        "hls",
        str(audio),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await create.communicate()
    assert create.returncode == 0, diagnostic.decode(errors="replace")
    async with httpx.AsyncClient() as http:
        chunks = media_chunks(
            http, Source(str(video), "hls", audio_url=str(audio)), QUALITY, ffmpeg
        )
        data = b"".join([chunk async for chunk in chunks])
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


async def test_missing_middle_hls_segment_never_completes_the_upload(tmp_path):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    manifest = tmp_path / "index.m3u8"
    create = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=32x32:r=10",
        "-t",
        "3",
        "-c:v",
        "libx264",
        "-g",
        "10",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "hls",
        "-hls_time",
        "1",
        str(manifest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await create.communicate()
    assert create.returncode == 0, diagnostic.decode(errors="replace")
    (tmp_path / "index1.ts").unlink()
    async with httpx.AsyncClient() as http:
        delivery = TelegramDelivery(None, http, ffmpeg)
        media = Media("xnxx", "demo", "Demo", "", 3, "page", None, (QUALITY,))
        from downloader_bot.schemas.media import DownloadError

        with pytest.raises(DownloadError, match="کامل نشد"):
            # The source fails before any completed file can be sent to Telegram.
            await delivery.new_file(None, media, QUALITY, Source(str(manifest), "hls"))


@pytest.mark.parametrize("mime_type", ["video/mp4", "audio/mp4"])
@pytest.mark.parametrize("site", ["xnxx", "xvideos"])
async def test_external_fallback_streams_with_headers_and_uses_correct_media_attributes(
    mime_type, site
):
    uploaded = []
    quality = Quality(
        "high", "High", "video", None, "mp4", mime_type, "progressive", "url", width=32, height=32
    )
    media = Media(site, "demo", "Demo", "", 1, "page", None, (quality,))
    peer = types.InputPeerUser(1, 2)

    class Client:
        async def __call__(self, request):
            assert request.bytes == b"media data"
            return True

        async def send_file(self, target, value, **kwargs):
            if isinstance(value, types.InputMediaDocumentExternal):
                raise errors.WebpageCurlFailedError(request=None)
            uploaded.append(value)
            return SimpleNamespace(
                id=3,
                document=SimpleNamespace(id=4, access_hash=5, file_reference=b"ref"),
            )

    def respond(request):
        assert request.headers["referer"] == f"https://www.{site}.com/"
        return httpx.Response(200, content=b"media data")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        file = await TelegramDelivery(Client(), http, "ffmpeg").new_file(
            peer,
            media,
            quality,
            Source(
                "https://cdn.example/video.mp4",
                "progressive",
                {"Referer": f"https://www.{site}.com/"},
            ),
        )
    assert file.document_id == 4
    assert uploaded[0].mime_type == mime_type
    attributes = uploaded[0].attributes
    assert any(isinstance(item, types.DocumentAttributeFilename) for item in attributes)
    assert any(isinstance(item, types.DocumentAttributeAudio) for item in attributes) == (
        mime_type == "audio/mp4"
    )
    video = next(
        (item for item in attributes if isinstance(item, types.DocumentAttributeVideo)), None
    )
    assert (video is not None) == (mime_type == "video/mp4")
    if video:
        assert (video.w, video.h) == (32, 32) and video.supports_streaming
