"""Prepare a complete, indexed MP4 before uploading a nonstreamable video."""

import asyncio
import shutil
import tempfile
from contextlib import aclosing, asynccontextmanager, nullcontext
from pathlib import Path

from bot.transfers.streaming import PART_SIZE, READ_SIZE
from schemas.media import DownloadError
from services.limits import check_file_size
from services.observability import acquired, current_transfer, timed

STAGING_BLOCK_SIZE = 256 * 1024


async def disk_call(function, *args):
    # Finish an in-flight disk operation before cancellation removes its workspace.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@asynccontextmanager
async def workspace():
    creating = asyncio.create_task(asyncio.to_thread(tempfile.mkdtemp, prefix="bot-video-"))
    try:
        directory = await asyncio.shield(creating)
    except asyncio.CancelledError:
        directory = await creating
        await disk_call(shutil.rmtree, directory)
        raise
    try:
        yield Path(directory)
    finally:
        await disk_call(shutil.rmtree, directory)


def append(path, data):
    with path.open("ab") as file:
        file.write(data)


def read_at(path, offset):
    with path.open("rb") as file:
        file.seek(offset)
        return file.read(STAGING_BLOCK_SIZE)


async def file_chunks(path):
    offset = 0
    while chunk := await disk_call(read_at, path, offset):
        offset += len(chunk)
        yield chunk


async def remux(ffmpeg, original, ready, limit, audio=None):
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file",
            "-i",
            str(original),
            *(["-protocol_whitelist", "file", "-i", str(audio)] if audio is not None else []),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0" if audio is not None else "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-fs",
            str(limit + 1),
            "-f",
            "mp4",
            str(ready),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise DownloadError("FFmpeg برای آماده‌سازی ویدیو تنظیم نشده است.") from error

    async def discard_errors():
        while await process.stderr.read(READ_SIZE):
            pass  # Signed URLs and unlimited diagnostics must never be retained.

    draining = asyncio.create_task(discard_errors())
    try:
        if await process.wait():
            raise DownloadError(
                "آماده‌سازی فایل برای پخش حین دانلود ناموفق بود؛ فایل ارسال نشد.",
                code="video_preparation_failed",
            )
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        await draining


@asynccontextmanager
async def prepared_video(
    chunks,
    ffmpeg,
    progress,
    max_file_bytes,
    *,
    input_slots=None,
    remux_slots=None,
    audio_chunks=None,
):
    # Even with the configurable cap disabled, respect the Telegram part limit.
    limit = max_file_bytes or 4000 * PART_SIZE
    async with workspace() as directory:
        original, ready = directory / "source.mp4", directory / "ready.mp4"
        size = 0
        audio = directory / "audio.mp4" if audio_chunks is not None else None

        async def stage(stream, path):
            nonlocal size
            buffer = bytearray()
            async with aclosing(stream):
                before = size
                async for chunk in stream:
                    size += len(chunk)
                    check_file_size(size, limit)
                    if audio_chunks is not None:
                        progress.downloaded = size
                    offset = 0
                    while offset < len(chunk):
                        take = min(STAGING_BLOCK_SIZE - len(buffer), len(chunk) - offset)
                        buffer.extend(memoryview(chunk)[offset : offset + take])
                        offset += take
                        if len(buffer) == STAGING_BLOCK_SIZE:
                            await disk_call(append, path, buffer)
                            buffer.clear()
                if buffer:
                    await disk_call(append, path, buffer)
                if size == before:
                    raise DownloadError("فایل دریافت‌شده خالی است.")

        if input_slots is not None:
            progress.phase = "waiting_process"
        async with (
            acquired(input_slots, "remux_wait") if input_slots is not None else nullcontext(),
            aclosing(chunks),
            aclosing(audio_chunks) if audio_chunks is not None else nullcontext(),
        ):
            progress.phase = "streaming"
            await stage(chunks, original)
            if audio_chunks is not None:
                progress.total = None
                await stage(audio_chunks, audio)
                if (trace := current_transfer.get()) is not None:
                    trace.source_size = size
        progress.phase = "waiting_process"
        async with (
            acquired(remux_slots, "remux_wait") if remux_slots is not None else nullcontext()
        ):
            progress.phase = "preparing_video"
            with timed("faststart_prepare"):
                if audio is None:
                    await remux(ffmpeg, original, ready, limit)
                else:
                    await remux(ffmpeg, original, ready, limit, audio)
        size = (await disk_call(ready.stat)).st_size
        check_file_size(size, limit)
        await disk_call(original.unlink)
        if audio is not None:
            await disk_call(audio.unlink)
        progress.downloaded = progress.total = size
        progress.download_done = True
        progress.phase = "streaming"
        async with aclosing(file_chunks(ready)) as stream:
            yield stream
