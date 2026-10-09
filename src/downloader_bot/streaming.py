import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing, nullcontext

import httpx
from telethon import functions, helpers, types

from downloader_bot.limits import check_file_size
from downloader_bot.models import DownloadError, Quality, Source
from downloader_bot.progress import TransferProgress
from downloader_bot.telemetry import current_transfer, timed

PART_SIZE = 512 * 1024
READ_SIZE = 64 * 1024


async def source_size(http: httpx.AsyncClient, source: Source) -> int | None:
    """Inspect response headers only; never buffer a media body to find its size."""
    async with AsyncExitStack() as stack:
        if source.proxy is not None:
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    proxy=source.proxy or None,
                    trust_env=False,
                    timeout=30,
                )
            )
        headers = {**source.headers, "Accept-Encoding": "identity"}
        for method in ("HEAD", "GET"):
            options = headers if method == "HEAD" else {**headers, "Range": "bytes=0-0"}
            async with http.stream(
                method,
                source.url,
                headers=options,
                follow_redirects=True,
            ) as response:
                if response.status_code >= 400:
                    continue
                if response.status_code == 206:
                    match = re.fullmatch(
                        r"bytes \d+-\d+/(\d+)", response.headers.get("content-range", "")
                    )
                    if match:
                        return int(match[1])
                else:
                    length = response.headers.get("content-length", "")
                    if length.isdigit():
                        return int(length)
    return None


def _input_options(source: Source, headers: dict[str, str], protocol: str) -> list[str]:
    options = []
    if headers:
        options.extend(
            ["-headers", "".join(f"{key}: {value}\r\n" for key, value in headers.items())]
        )
    if source.proxy:
        options.extend(["-http_proxy", source.proxy])
    if protocol == "hls":
        # Some FFmpeg versions stop fMP4 demuxing after the first ranged segment.
        options.extend(["-http_seekable", "0"])
    return options


def remux_arguments(source: Source, quality: Quality) -> list[str]:
    """Build a codec-copy command; media discovery stays inside each provider."""
    inputs = [
        *_input_options(source, source.headers, source.input_protocol or source.protocol),
        "-i",
        source.url,
    ]
    if source.audio_url:
        inputs.extend(
            [
                *_input_options(
                    source,
                    source.audio_headers or source.headers,
                    source.audio_protocol or source.protocol,
                ),
                "-rw_timeout",
                "30000000",
                "-i",
                source.audio_url,
            ]
        )

    if quality.mime_type.startswith("video/"):
        audio_map = "0:a:0" if source.require_audio else "0:a:0?"
        if source.audio_url:
            audio_map = "1:a:0"
        tracks = ["-map", "0:v:0", "-map", audio_map, "-c", "copy"]
    else:
        tracks = ["-map", "0:a:0", "-vn", "-c:a", "copy"]

    if quality.codec == "mp3":
        output = ["-f", "mp3"]
    elif quality.extension in {"opus", "ogg"}:
        output = ["-f", "ogg"]
    elif quality.extension == "webm":
        output = ["-f", "webm"]
    else:
        output = [
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+default_base_moof",
            "-frag_duration",
            "1000000",
        ]
    return [
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-progress",
        "pipe:2",
        "-stats_period",
        "0.5",
        "-rw_timeout",
        "30000000",
        *inputs,
        *tracks,
        *output,
        "pipe:1",
    ]


async def media_chunks(
    http: httpx.AsyncClient,
    source: Source,
    quality: Quality,
    ffmpeg: str,
    progress: TransferProgress | None = None,
) -> AsyncIterator[bytes]:
    trace = current_transfer.get()
    if trace is not None:
        trace.start_phase("download")
    try:
        with timed("source_stream"):
            async with aclosing(_media_chunks(http, source, quality, ffmpeg, progress)) as chunks:
                async for chunk in chunks:
                    if trace is not None:
                        trace.add("stream_read_bytes", len(chunk))
                        key = (
                            "progressive_download_bytes"
                            if source.protocol == "progressive"
                            else "ffmpeg_output_bytes"
                        )
                        trace.add(key, len(chunk))
                    yield chunk
    finally:
        if trace is not None:
            trace.end_phase("download")


async def _media_chunks(
    http: httpx.AsyncClient,
    source: Source,
    quality: Quality,
    ffmpeg: str,
    progress: TransferProgress | None,
) -> AsyncIterator[bytes]:
    if source.protocol == "progressive":
        async with AsyncExitStack() as stack:
            if source.proxy is not None:
                http = await stack.enter_async_context(
                    httpx.AsyncClient(
                        proxy=source.proxy or None,
                        trust_env=False,
                        timeout=30,
                    )
                )
            async with aclosing(progressive_chunks(http, source, progress)) as chunks:
                async for chunk in chunks:
                    yield chunk
        return
    if source.protocol not in {"hls", "dash"}:
        raise DownloadError("روش انتقال این کیفیت پشتیبانی نشده است.")
    # Remux in a separate process without re-encoding the selected media.
    environment = None
    if source.proxy is not None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        }
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            *remux_arguments(source, quality),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
    except OSError as error:
        raise DownloadError("FFmpeg برای دریافت این کیفیت روی سرور تنظیم نشده است.") from error

    skipped_segment = False
    media_seconds = 0.0

    async def drain_errors() -> None:
        nonlocal skipped_segment, media_seconds
        # Never buffer unlimited logs, which may also contain temporary signed URLs.
        while line := await process.stderr.readline():
            if b"Failed to open segment" in line or b"Failed to reload playlist" in line:
                skipped_segment = True
            if line.startswith(b"out_time_us="):
                value = line.partition(b"=")[2].strip()
                if value.isdigit():
                    media_seconds = max(media_seconds, int(value) / 1_000_000)
                    if progress is not None:
                        progress.seconds = media_seconds

    errors = asyncio.create_task(drain_errors())
    try:
        while chunk := await process.stdout.read(READ_SIZE):
            if progress is not None:
                progress.downloaded += len(chunk)
            yield chunk
        returncode = await process.wait()
        await errors
        incomplete = source.duration and media_seconds < source.duration - max(
            1, source.duration * 0.005
        )
        if returncode != 0 or skipped_segment or incomplete:
            raise DownloadError(
                "دریافت جریان رسانه کامل نشد؛ فایل ناقص ارسال نمی‌شود.",
                code="media_stream_incomplete",
            )
        if progress is not None:
            progress.download_done = True
            progress.total = progress.downloaded
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        await errors


async def progressive_chunks(
    http: httpx.AsyncClient,
    source: Source,
    progress: TransferProgress | None,
) -> AsyncIterator[bytes]:
    """Use small byte ranges for YouTube; verify every response before finalizing."""
    offset, total = 0, source.size_bytes
    trace = current_transfer.get()
    while True:
        headers = {**source.headers, "Accept-Encoding": "identity"}
        end = None
        if source.chunk_size:
            end = offset + source.chunk_size - 1
            if total:
                end = min(end, total - 1)
            headers["Range"] = f"bytes={offset}-{end}"
        async with http.stream(
            "GET",
            source.url,
            headers=headers,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            ranged = response.status_code == 206
            expected = None
            if ranged:
                match = re.fullmatch(
                    r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", "")
                )
                if not match:
                    raise DownloadError("پاسخ محدوده فایل معتبر نیست؛ فایل ناقص ارسال نمی‌شود.")
                start, stop, length = map(int, match.groups())
                if (
                    start != offset
                    or stop < start
                    or stop >= length
                    or (end is not None and stop > end)
                ):
                    raise DownloadError("محدوده دریافت فایل یکسان نیست؛ فایل ناقص ارسال نمی‌شود.")
                if total is not None and total != length:
                    raise DownloadError("اندازه فایل تغییر کرده؛ لینک را دوباره بفرست.")
                total, expected = length, stop - start + 1
            elif offset:
                raise DownloadError("سرور ادامه محدوده فایل را نپذیرفت؛ فایل ناقص ارسال نمی‌شود.")
            else:
                length = response.headers.get("content-length", "")
                if length.isdigit():
                    expected = int(length)
                    if total is not None and total != expected:
                        raise DownloadError("اندازه فایل تغییر کرده؛ لینک را دوباره بفرست.")
                    total = expected
            if progress is not None:
                progress.total = total
            if trace is not None:
                trace.source_size = total
            received = 0
            async for chunk in response.aiter_bytes(READ_SIZE):
                received += len(chunk)
                if expected is not None and received > expected:
                    raise DownloadError("پاسخ دریافت فایل بیش از اندازه اعلام‌شده است.")
                if progress is not None:
                    progress.downloaded += len(chunk)
                yield chunk
            if expected is not None and received != expected:
                raise DownloadError("دریافت فایل کامل نشد؛ فایل ناقص ارسال نمی‌شود.")
            offset += received
            if not ranged or offset == total:
                if total is not None and offset != total:
                    raise DownloadError("دریافت فایل کامل نشد؛ فایل ناقص ارسال نمی‌شود.")
                break
            if not source.chunk_size or not received:
                raise DownloadError("دریافت فایل کامل نشد؛ فایل ناقص ارسال نمی‌شود.")
    if progress is not None:
        progress.download_done = True
        progress.total = offset


async def upload_stream(
    client,
    chunks: AsyncIterator[bytes],
    name: str,
    max_parts: int = 4000,
    progress: TransferProgress | None = None,
    parallelism: int = 1,
    part_timeout: float = 45,
    max_file_bytes: int = 0,
    upload_slots: asyncio.Semaphore | None = None,
):
    """Telegram's unknown-length protocol; bounded RAM and no local media file.

    https://core.telegram.org/api/files#streamed-uploads
    The final empty part is required when the stream ends on a part boundary.
    """
    if not 1 <= parallelism <= 8 or max_parts < 2 or part_timeout <= 0:
        raise ValueError("Invalid upload window, part limit or timeout")
    file_id = helpers.generate_random_long()
    index = 0
    received = 0
    buffer = bytearray()
    buffer_slot = False
    pending: set[asyncio.Task] = set()
    trace = current_transfer.get()

    async def send(part_index: int, total: int, data: bytes) -> None:
        if trace is not None:
            trace.start_phase("upload")
        async with asyncio.timeout(part_timeout):
            if not await client(
                functions.upload.SaveBigFilePartRequest(file_id, part_index, total, data)
            ):
                raise DownloadError("آپلود یکی از قسمت‌های فایل ناموفق بود.")
        if trace is not None:
            trace.add("upload_acked_bytes", len(data))
        if progress is not None:
            progress.uploaded += len(data)

    async def collect(tasks) -> None:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    try:
        async with aclosing(chunks):
            async for chunk in chunks:
                received += len(chunk)
                check_file_size(received, max_file_bytes)
                offset = 0
                while offset < len(chunk):
                    if not buffer:
                        if len(pending) >= parallelism:
                            done, pending = await asyncio.wait(
                                pending,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            await collect(done)
                        # Bound unfinished buffers as well as RPC payloads. Each of
                        # the other open sources retains only one small read chunk.
                        if upload_slots is not None:
                            with timed("upload_window_wait"):
                                await upload_slots.acquire()
                            buffer_slot = True
                    count = min(PART_SIZE - len(buffer), len(chunk) - offset)
                    buffer.extend(memoryview(chunk)[offset : offset + count])
                    offset += count
                    if len(buffer) == PART_SIZE:
                        if index >= max_parts - 1:
                            raise DownloadError("حجم فایل از سقف تنظیم‌شده برای انتقال بیشتر است.")
                        task = asyncio.create_task(send(index, -1, bytes(buffer)))
                        if upload_slots is not None:
                            # Also releases if cancelled before the coroutine's first step.
                            task.add_done_callback(lambda _: upload_slots.release())
                            buffer_slot = False
                        pending.add(task)
                        buffer.clear()
                        index += 1
                        if parallelism == 1:
                            await collect(pending)
                            pending.clear()
        count = index + bool(buffer)
        if count == 0:
            raise DownloadError("جریان دریافت‌شده خالی است.")
        await collect(pending)
        pending.clear()
        # Finalize only after every full part has been acknowledged and EOF is valid.
        async with upload_slots if upload_slots is not None and not buffer_slot else nullcontext():
            await send(index, count, bytes(buffer))
        if progress is not None:
            progress.upload_done = True
        return types.InputFileBig(file_id, count, name)
    finally:
        if buffer_slot:
            upload_slots.release()
        for task in pending:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if trace is not None:
            trace.end_phase("upload")
