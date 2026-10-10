"""Exercise a seekable player while most video bytes remain unavailable."""

import asyncio
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from test_video_streaming import MEDIA, PEER, QUALITY, TelegramMock

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.progress import TransferProgress
from downloader_bot.bot.transfers.faststart import prepared_video
from downloader_bot.bot.transfers.video import mp4_header
from downloader_bot.schemas.media import DownloadError, Source, TelegramFile
from downloader_bot.services.download import DownloadService


@pytest.fixture
async def neutral_video(tmp_path):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    async def run(*arguments):
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_path,
        )
        output, diagnostic = await process.communicate()
        assert process.returncode == 0, diagnostic.decode(errors="replace")
        return output

    tail, fast = tmp_path / "tail.mp4", tmp_path / "fast.mp4"
    await run(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=640x360:r=25",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-b:v",
        "1200k",
        "-g",
        "50",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(tail),
    )
    await run("-i", str(tail), "-c", "copy", "-movflags", "+faststart", str(fast))
    fragmented = await run(
        "-i",
        str(tail),
        "-c",
        "copy",
        "-movflags",
        "frag_keyframe+default_base_moof",
        "-frag_duration",
        "1000000",
        "-f",
        "mp4",
        "pipe:1",
    )
    await run(
        "-i",
        str(tail),
        "-c",
        "copy",
        "-hls_time",
        "2",
        "-hls_playlist_type",
        "vod",
        "-hls_segment_type",
        "fmp4",
        str(tmp_path / "video.m3u8"),
    )
    files = {"/" + path.name: path.read_bytes() for path in tmp_path.iterdir()}
    files["/fragmented.mp4"] = fragmented
    return ffmpeg, files


@asynccontextmanager
async def video_server(files, *, available=None):
    tasks = set()
    blocked = asyncio.Event()
    state = {"served": 0, "ranges": [], "blocked": blocked}

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            request = (await reader.readuntil(b"\r\n\r\n")).decode("latin1")
            method, path, _ = request.splitlines()[0].split()
            data = files[path]
            match = re.search(r"(?im)^range: bytes=(\d+)-(\d*)", request)
            start = int(match[1]) if match else 0
            stop = min(len(data) - 1, int(match[2])) if match and match[2] else len(data) - 1
            state["ranges"].append((start, stop))
            status = "206 Partial Content" if match else "200 OK"
            header = (
                f"HTTP/1.1 {status}\r\nAccept-Ranges: bytes\r\nConnection: close\r\n"
                f"Content-Length: {stop - start + 1}\r\n"
            )
            if match:
                header += f"Content-Range: bytes {start}-{stop}/{len(data)}\r\n"
            writer.write(header.encode() + b"\r\n")
            await writer.drain()
            if method == "HEAD":
                return
            offset = start
            while offset <= stop:
                if available is not None and offset >= available:
                    blocked.set()
                    await asyncio.Event().wait()  # Tail bytes never become available.
                end = min(offset + 16384, stop + 1)
                if available is not None:
                    end = min(end, available)
                writer.write(data[offset:end])
                await writer.drain()
                state["served"] += end - offset
                offset = end
                await asyncio.sleep(0.001)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            tasks.discard(task)

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}", state
        finally:
            active = list(tasks)
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)


async def play_prefix(ffmpeg, data, *, expect_blocked=False):
    available = len(data) // 3
    async with video_server({"/video.mp4": data}, available=available) as (url, state):
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            url + "/video.mp4",
            "-t",
            "5",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "framemd5",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        frames = [0, 0]

        async def read_frames():
            while line := await process.stdout.readline():
                if line.startswith(b"0,"):
                    frames[0] += 1
                elif line.startswith(b"1,"):
                    frames[1] += 1

        reading = asyncio.create_task(read_frames())
        diagnostics = asyncio.create_task(process.stderr.read())
        try:
            async with asyncio.timeout(8):
                if expect_blocked:
                    await state["blocked"].wait()
                    assert frames == [0, 0]
                else:
                    assert await process.wait() == 0
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            await reading
            error = await diagnostics
        if not expect_blocked:
            assert frames[0] >= 120 and frames[1] > 200, error.decode(errors="replace")
        assert state["served"] < len(data)
        return state, frames


@pytest.mark.parametrize(
    "protocol,path",
    [
        ("progressive", "/fast.mp4"),
        ("progressive", "/tail.mp4"),
        ("progressive", "/fragmented.mp4"),
        ("dash", "/tail.mp4"),
        ("hls", "/video.m3u8"),
    ],
)
async def test_uploaded_video_plays_five_seconds_with_two_thirds_unavailable(
    neutral_video, protocol, path
):
    ffmpeg, files = neutral_video
    progress = TransferProgress()

    class CompleteFirst(TelegramMock):
        async def __call__(self, request):
            if protocol != "progressive" or path != "/fast.mp4":
                if hasattr(request, "file_part"):
                    assert progress.download_done  # Download/remux finished before upload.
            return await super().__call__(request)

    client = CompleteFirst()
    async with video_server(files) as (url, _), httpx.AsyncClient(trust_env=False) as http:
        file = await TelegramDelivery(
            client, http, ffmpeg, max_file_bytes=8 * 1024 * 1024
        ).new_file(
            PEER,
            replace(MEDIA, duration=30),
            QUALITY,
            Source(url + path, protocol, require_audio=True),
            progress,
        )
    uploaded = b"".join(client.parts[index] for index in sorted(client.parts))
    assert file.video_faststart and file.verified_complete
    assert mp4_header(uploaded)[1].faststart
    if path == "/fast.mp4":
        assert uploaded == files[path]
    await play_prefix(ffmpeg, uploaded)


async def test_previous_fragmented_output_needs_unavailable_future_fragments(neutral_video):
    ffmpeg, files = neutral_video
    assert not mp4_header(files["/fragmented.mp4"])[1].faststart
    await play_prefix(ffmpeg, files["/fragmented.mp4"], expect_blocked=True)


async def test_old_fragmented_cache_is_rebuilt_once_even_with_preview_and_streaming_flags(
    neutral_video,
):
    ffmpeg, files = neutral_video
    stored = TelegramFile(1, 2, b"ref", bytes(PEER), 3, 1024, True, True, True)
    client = TelegramMock()
    client.remote_data = files["/fragmented.mp4"]
    resolves = 0

    async def get(*args):
        return stored

    async def save(*args):
        nonlocal stored
        stored = args[-1]

    async with video_server(files) as (url, _), httpx.AsyncClient(trust_env=False) as http:

        async def resolve(*args):
            nonlocal resolves
            resolves += 1
            return Source(url + "/tail.mp4", "progressive")

        from types import SimpleNamespace

        service = DownloadService(
            SimpleNamespace(resolve=resolve),
            SimpleNamespace(get=get, save=save, key=lambda *a: "id"),
            TelegramDelivery(client, http, ffmpeg),
        )
        assert await service.deliver(PEER, replace(MEDIA, duration=30), QUALITY) == "transferred"
        assert await service.deliver(PEER, replace(MEDIA, duration=30), QUALITY) == "reused"
    assert resolves == client.header_reads == 1 and stored.video_faststart


async def test_temporary_download_is_removed_on_cancel(tmp_path, monkeypatch):
    directory = tmp_path / "work"
    directory.mkdir()
    monkeypatch.setattr(
        "downloader_bot.bot.transfers.faststart.tempfile.mkdtemp", lambda **kw: str(directory)
    )
    writing, closed = asyncio.Event(), asyncio.Event()

    async def chunks():
        try:
            yield b"sample"
            writing.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def transfer():
        async with prepared_video(chunks(), "unused", TransferProgress(), 1024):
            pytest.fail("An incomplete download must not be uploaded")

    task = asyncio.create_task(transfer())
    await writing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and not directory.exists()


async def test_temporary_download_is_removed_when_size_limit_rejects_it(tmp_path, monkeypatch):
    directory = tmp_path / "work"
    directory.mkdir()
    monkeypatch.setattr(
        "downloader_bot.bot.transfers.faststart.tempfile.mkdtemp", lambda **kw: str(directory)
    )

    async def chunks():
        yield b"x" * 1025

    with pytest.raises(DownloadError):
        async with prepared_video(chunks(), "unused", TransferProgress(), 1024):
            pytest.fail("An oversized download must not be uploaded")
    assert not directory.exists()


async def test_slow_disk_write_does_not_block_loop_and_cancel_waits_before_cleanup(
    tmp_path, monkeypatch
):
    from downloader_bot.bot.transfers import faststart

    directory = tmp_path / "work"
    directory.mkdir()
    monkeypatch.setattr(faststart.tempfile, "mkdtemp", lambda **kw: str(directory))
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def slow_append(path, data):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        assert path.parent.exists()  # Cancellation cannot delete under an active write.
        path.write_bytes(data)

    monkeypatch.setattr(faststart, "append", slow_append)

    async def chunks():
        yield b"sample"

    async def transfer():
        async with prepared_video(chunks(), "unused", TransferProgress(), 1024):
            pytest.fail("A cancelled write must not be uploaded")

    task = asyncio.create_task(transfer())
    try:
        async with asyncio.timeout(2):
            await started.wait()
        task.cancel()
        # Timers and other tasks still advance while the disk worker is blocked.
        await asyncio.sleep(0.02)
        assert not task.done() and directory.exists()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not directory.exists()
