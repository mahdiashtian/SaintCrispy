"""Decode one video frame in a separate process using a bounded in-memory prefix."""

import asyncio

from bot.transfers.video import MAX_PREPARED_HEADER_BYTES, mp4_header
from schemas.media import DownloadError

MAX_PREVIEW_INPUT = 1024 * 1024
MAX_PREVIEW_BYTES = 20 * 1024


async def video_thumbnail(ffmpeg: str, prefix: bytes) -> bytes:
    _, info = mp4_header(prefix, MAX_PREPARED_HEADER_BYTES)
    limit = MAX_PREVIEW_INPUT
    if info:
        limit += min(info.prefix_size, MAX_PREPARED_HEADER_BYTES)
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            "scale=320:320:force_original_aspect_ratio=decrease",
            "-q:v",
            "8",
            "-f",
            "image2pipe",
            "-c:v",
            "mjpeg",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as error:
        raise DownloadError("FFmpeg برای آماده‌سازی پیش‌نمایش ویدیو تنظیم نشده است.") from error
    try:
        async with asyncio.timeout(15):
            jpeg, _ = await process.communicate(prefix[:limit])
        if (
            process.returncode
            or not jpeg.startswith(b"\xff\xd8")
            or not jpeg.endswith(b"\xff\xd9")
            or len(jpeg) > MAX_PREVIEW_BYTES
        ):
            raise DownloadError(
                "پیش‌نمایش ویدیو آماده نشد؛ فایل را با کیفیت دیگری امتحان کن.",
                code="video_preview_failed",
            )
        return jpeg
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
