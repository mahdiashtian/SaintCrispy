import asyncio
import re
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from telethon import types
from test_video_streaming import PEER, TelegramMock
from video_fixture import box, mp4_header

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.progress import TransferProgress
from downloader_bot.bot.transfers.integrity import MP4Integrity
from downloader_bot.schemas.media import DownloadError, Media, Quality, Source

QUALITY = Quality("full", "Full", "h264", None, "mp4", "video/mp4", "progressive", "")


@pytest.fixture
async def integrity_origin(tmp_path):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    async def create(name, args):
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            *args,
            str(tmp_path / name),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, diagnostic = await process.communicate()
        assert process.returncode == 0, diagnostic.decode(errors="replace")

    await create(
        "full.mp4",
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=80x48:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "18",
            "-c:v",
            "libx264",
            "-g",
            "10",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
        ],
    )
    await create(
        "short.mp4",
        [
            "-i",
            str(tmp_path / "full.mp4"),
            "-t",
            "11",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
        ],
    )
    await create(
        "video.mp4",
        [
            "-i",
            str(tmp_path / "short.mp4"),
            "-an",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
        ],
    )
    await create(
        "audio.m4a",
        ["-i", str(tmp_path / "full.mp4"), "-vn", "-c", "copy", "-movflags", "+faststart"],
    )
    await create(
        "short-audio.m4a",
        ["-i", str(tmp_path / "short.mp4"), "-vn", "-c", "copy", "-movflags", "+faststart"],
    )
    await create("audio.webm", ["-i", str(tmp_path / "full.mp4"), "-vn", "-c:a", "libopus"])
    files = {"/" + p.name: p.read_bytes() for p in tmp_path.iterdir()}
    requests = []

    async def respond(reader, writer):
        try:
            request = (await reader.readuntil(b"\r\n\r\n")).decode("latin1")
            method, path, _ = request.splitlines()[0].split()
            requests.append(request)
            body = files[path]
            start, stop = 0, len(body) - 1
            match = re.search(r"(?im)^range: bytes=(\d+)-(\d*)", request)
            if match:
                start = int(match[1])
                stop = min(stop, int(match[2])) if match[2] else stop
            headers = f"HTTP/1.1 {'206 Partial Content' if match else '200 OK'}\r\n"
            headers += f"Content-Length: {stop - start + 1}\r\nConnection: close\r\n"
            if match:
                headers += f"Content-Range: bytes {start}-{stop}/{len(body)}\r\n"
            writer.write(headers.encode() + b"\r\n")
            if method != "HEAD":
                writer.write(body[start : stop + 1])
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
            files=files,
            ffmpeg=ffmpeg,
            requests=requests,
        )


async def decoded_seconds(ffmpeg, data, maps):
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-i",
        "pipe:0",
        *maps,
        "-f",
        "null",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, diagnostic = await process.communicate(data)
    assert process.returncode == 0, diagnostic.decode(errors="replace")
    return max(int(t) for t in re.findall(rb"out_time_us=(\d+)", output)) / 1_000_000


@pytest.mark.parametrize(
    "site", ["soundcloud", "youtube", "instagram", "pinterest", "xvideos", "xnxx"]
)
async def test_each_provider_delivery_rejects_11_second_external_preview_and_sends_full_video(
    integrity_origin,
    site,
):
    origin = integrity_origin
    media = Media(site, "id", "Public test", "", 18, "page", None, (QUALITY,))
    document = SimpleNamespace(
        id=10,
        access_hash=20,
        file_reference=b"preview",
        size=len(origin.files["/full.mp4"]),
        attributes=[types.DocumentAttributeVideo(11, 80, 48, supports_streaming=True)],
    )
    client = TelegramMock(document)
    async with httpx.AsyncClient(trust_env=False) as http:
        file = await TelegramDelivery(client, http, origin.ffmpeg).new_file(
            PEER,
            media,
            QUALITY,
            Source(origin.url + "/full.mp4", "progressive", require_audio=True),
        )
    assert file.verified_complete and len(client.messages) == 1
    assert isinstance(client.messages[0], types.InputMediaUploadedDocument)
    data = b"".join(client.parts[i] for i in sorted(client.parts))
    assert data == origin.files["/full.mp4"]
    assert await decoded_seconds(origin.ffmpeg, data, ["-map", "0:v:0", "-map", "0:a:0"]) >= 17.9


async def test_origin_11_second_preview_is_never_finalized_or_published(integrity_origin):
    origin = integrity_origin
    client = TelegramMock()
    media = Media("instagram", "id", "Public test", "", 18, "page", None, (QUALITY,))
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(DownloadError) as failure:
            await TelegramDelivery(client, http, origin.ffmpeg).new_file(
                PEER,
                media,
                QUALITY,
                Source(origin.url + "/short.mp4", "progressive"),
            )
    assert failure.value.code == "media_stream_incomplete" and not client.messages


async def test_full_audio_cannot_conceal_a_video_ending_after_11_seconds(integrity_origin):
    origin = integrity_origin
    quality = replace(QUALITY, protocol="dash")
    media = Media("instagram", "id", "Public test", "", 18, "page", None, (quality,))
    client = TelegramMock()
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(DownloadError) as failure:
            await TelegramDelivery(client, http, origin.ffmpeg).new_file(
                PEER,
                media,
                quality,
                Source(
                    origin.url + "/video.mp4",
                    "dash",
                    audio_url=origin.url + "/audio.m4a",
                    require_audio=True,
                ),
            )
    assert failure.value.code == "media_stream_incomplete" and not client.messages


@pytest.mark.parametrize("full", [False, True])
async def test_real_audio_preserves_full_length_and_rejects_preview(integrity_origin, full):
    origin = integrity_origin
    quality = replace(QUALITY, codec="aac", extension="m4a", mime_type="audio/mp4")
    media = Media("soundcloud", "id", "Public audio", "", 18, "page", None, (quality,))
    client = TelegramMock()
    progress = TransferProgress()
    async with httpx.AsyncClient(trust_env=False) as http:
        operation = TelegramDelivery(client, http, origin.ffmpeg).new_file(
            PEER,
            media,
            quality,
            Source(origin.url + ("/audio.m4a" if full else "/short-audio.m4a"), "progressive"),
            progress,
        )
        if not full:
            with pytest.raises(DownloadError) as failure:
                await operation
            assert failure.value.code == "media_stream_incomplete" and not client.messages
            return
        assert (await operation).verified_complete
    data = b"".join(client.parts[i] for i in sorted(client.parts))
    assert await decoded_seconds(origin.ffmpeg, data, ["-map", "0:a:0"]) >= 17.9
    assert progress.seconds >= 17.9


@pytest.mark.parametrize("mime,extension", [("audio/mp4", "m4a"), ("audio/webm", "webm")])
async def test_progressive_audio_validation_preserves_required_small_http_ranges(
    integrity_origin, mime, extension
):
    origin = integrity_origin
    quality = replace(QUALITY, mime_type=mime, extension=extension)
    media = Media("youtube", "id", "Public audio", "", 18, "page", None, (quality,))
    client = TelegramMock()
    async with httpx.AsyncClient(trust_env=False) as http:
        reference = await TelegramDelivery(client, http, origin.ffmpeg).new_file(
            PEER,
            media,
            quality,
            Source(origin.url + f"/audio.{extension}", "progressive", chunk_size=4096),
        )
    assert reference.verified_complete
    assert len(origin.requests) > 2 and all("Range: bytes=" in r for r in origin.requests)
    data = b"".join(client.parts[i] for i in sorted(client.parts))
    assert await decoded_seconds(origin.ffmpeg, data, ["-map", "0:a:0"]) >= 17.9


async def test_fragmented_vp9_includes_samples_in_the_initial_moov(tmp_path):
    import imageio_ffmpeg

    from downloader_bot.bot.transfers.streaming import media_chunks

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    video = tmp_path / "vp9.mp4"
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=80x48:r=30",
        "-t",
        "11.7",
        "-an",
        "-c:v",
        "libvpx-vp9",
        str(video),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await process.communicate()
    assert process.returncode == 0, diagnostic.decode(errors="replace")
    quality = replace(QUALITY, codec="vp9", protocol="dash")
    validator = MP4Integrity()
    async with httpx.AsyncClient() as http:
        data = b"".join(
            [
                c
                async for c in media_chunks(
                    http,
                    Source(str(video), "dash", duration=12),
                    quality,
                    ffmpeg,
                )
            ]
        )
    validator.feed(data)
    duration = validator.finish(12)
    track = next(t for t in validator.tracks.values() if t.kind == b"vide")
    assert track.duration > 0 and track.fragment_ticks > 0
    assert duration == pytest.approx(
        await decoded_seconds(ffmpeg, data, ["-map", "0:v:0"]), abs=0.05
    )


async def test_cancelling_ranged_audio_closes_http_feeder_and_ffmpeg(integrity_origin, monkeypatch):
    from downloader_bot.bot.transfers.streaming import media_chunks

    ready, closed = asyncio.Event(), asyncio.Event()
    process = None
    original_start = asyncio.create_subprocess_exec

    async def start(*args, **kwargs):
        nonlocal process
        process = await original_start(*args, **kwargs)
        return process

    monkeypatch.setattr(
        "downloader_bot.bot.transfers.streaming.asyncio.create_subprocess_exec", start
    )

    class WaitingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                ready.set()
                await asyncio.Future()
                yield b"unused"
            finally:
                closed.set()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=WaitingBody()))
    ) as http:
        quality = replace(QUALITY, mime_type="audio/webm", extension="webm")

        async def consume():
            async for _ in media_chunks(
                http,
                Source("https://cdn.example/audio", "progressive", chunk_size=4096),
                quality,
                integrity_origin.ffmpeg,
                remux_progressive=True,
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert closed.is_set() and process.returncode is not None


def test_mp4_validation_rejects_partial_boxes_even_if_http_length_looks_complete():
    data = mp4_header(duration=18) + box(b"mdat", b"samples")
    for partial in (data[:-1], data + b"\0\0", data[:30], mp4_header(duration=18)):
        validator = MP4Integrity()
        for offset in range(0, len(partial), 7):
            validator.feed(partial[offset : offset + 7])
        with pytest.raises(DownloadError):
            validator.finish(18)
    validator = MP4Integrity()
    for offset in range(0, len(data), 7):
        validator.feed(data[offset : offset + 7])
    assert validator.finish(18) == 18
