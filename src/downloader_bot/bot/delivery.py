import asyncio
import re
import time
from contextlib import nullcontext
from dataclasses import replace

import httpx
from telethon import TelegramClient, errors, functions, types, utils
from telethon.extensions import BinaryReader

from downloader_bot.bot.progress import TransferProgress
from downloader_bot.bot.transfers.streaming import media_chunks, source_size, upload_stream
from downloader_bot.bot.transfers.video import inspect_mp4, require_video_info
from downloader_bot.core.request_context import request_chat, request_message
from downloader_bot.schemas.media import (
    DownloadError,
    Media,
    Quality,
    Source,
    TelegramFile,
    streamable_video,
)
from downloader_bot.services.limits import check_file_size
from downloader_bot.services.observability import acquired, count, current_transfer, timed

EXTERNAL_FAILURES = (
    errors.ExternalUrlInvalidError,
    errors.WebpageCurlFailedError,
    errors.WebpageMediaEmptyError,
    errors.MediaEmptyError,
)
REFERENCE_FAILURES = (
    errors.FileReferenceExpiredError,
    errors.FileReferenceInvalidError,
    errors.FileReferenceEmptyError,
)


class ExternalFetchTimeout(Exception):
    """Only an unpublished URL registration timed out, so fallback is safe."""


class ExternalFileMismatch(Exception):
    """Telegram's URL cache returned a different size from the resolved origin."""


class ExternalVideoNotStreamable(Exception):
    """The unpublished URL registration needs local preparation before publication."""


class TelegramDelivery:
    def __init__(
        self,
        client: TelegramClient,
        http: httpx.AsyncClient,
        ffmpeg: str,
        upload_parallelism: int = 1,
        remux_concurrency: int = 2,
        sends_per_second: float = 0,
        max_file_bytes: int = 0,
        upload_inflight_parts: int = 64,
        external_timeout: float = 30,
    ):
        self.client = client
        self.http = http
        self.ffmpeg = ffmpeg
        self.upload_parallelism = upload_parallelism
        self.max_file_bytes = max_file_bytes
        self.external_timeout = external_timeout
        self._remux_slots = asyncio.Semaphore(remux_concurrency)
        if upload_inflight_parts < 1:
            raise ValueError("Global upload window must be positive")
        self._upload_slots = asyncio.Semaphore(upload_inflight_parts)
        self._send_interval = 1 / sends_per_second if sends_per_second else 0
        self._next_send = 0.0
        self._retry_at = 0.0
        self._gate = asyncio.Lock()

    async def _request(self, action, *, publish: bool = False, progress=None):
        """Pace media sends and share Telegram's explicit cooldown across workers."""
        while True:
            while True:
                async with self._gate:
                    now = time.monotonic()
                    ready = max(self._retry_at, self._next_send if publish else 0)
                    delay = ready - now
                    if delay <= 0:
                        if publish:
                            self._next_send = now + self._send_interval
                            if progress is not None:
                                progress.phase = "publishing"
                        break
                with timed("telegram_wait"):
                    await asyncio.sleep(delay)
            try:
                return await action()
            except errors.FloodWaitError as error:
                if (trace := current_transfer.get()) is not None:
                    trace.discard_error(error)
                count("flood_wait_events")
                count("flood_wait_requested_seconds", error.seconds)
                # Only an explicit rejected RPC is repeated; ambiguous network failures are not.
                self._retry_at = max(self._retry_at, time.monotonic() + max(1, error.seconds))
                if progress is not None:
                    progress.phase = "waiting_telegram"

    async def _send_file(self, peer, media, *, progress=None, **options):
        message_id = request_message.get()
        if message_id:
            try:
                same_chat = utils.get_peer_id(peer) == request_chat.get()
            except (TypeError, ValueError):
                same_chat = False
            if same_chat:
                options.setdefault("reply_to", message_id)
        if progress is not None:
            progress.phase = "waiting_telegram"
        with timed("publish"):
            result = await self._request(
                lambda: self.client.send_file(peer, media, **options),
                publish=True,
                progress=progress,
            )
        count("telegram_publications")
        return result

    async def _upload_part(self, request):
        # Part RPCs share the same explicit Telegram cooldown as file publication.
        async def send():
            count("upload_part_attempts")
            count("upload_attempt_bytes", len(request.bytes))
            with timed("upload_rpc"):
                return await self.client(request)

        return await self._request(send)

    async def _fetch_external(self, peer, external):
        try:
            with timed("external_fetch"):
                async with asyncio.timeout(self.external_timeout):
                    return await self._request(
                        lambda: self.client(functions.messages.UploadMediaRequest(peer, external)),
                    )
        except TimeoutError as error:
            raise ExternalFetchTimeout from error

    async def new_file(
        self,
        peer,
        media: Media,
        quality: Quality,
        source: Source,
        progress: TransferProgress | None = None,
    ) -> TelegramFile:
        if quality.duration is not None:
            media = replace(media, duration=quality.duration)
        caption = f"{media.title}\n{media.artist}\n{quality.label}"
        if (trace := current_transfer.get()) is not None:
            trace.protocol = source.protocol
            trace.source_size = source.size_bytes
        with timed("size_probe"):
            size = await self._checked_source_size(source)
        if trace is not None:
            trace.source_size = size
        # Unknown-size sources use the byte-capped stream instead of unrestricted URL fetching.
        if source.protocol == "progressive" and (not self.max_file_bytes or size is not None):
            reference = await self._try_external(peer, source, caption, size, progress, quality)
            if reference is not None:
                return reference
        return await self._stream_file(peer, media, quality, source, caption, progress)

    async def _checked_source_size(self, source: Source) -> int | None:
        check_file_size(source.size_bytes, self.max_file_bytes)
        size = source.size_bytes
        if source.protocol == "progressive" and self.max_file_bytes and size is None:
            try:
                size = await source_size(self.http, source)
            except (httpx.HTTPError, TimeoutError):
                # Unknown sizes still use the byte-capped streaming path below.
                size = None
            check_file_size(size, self.max_file_bytes)
        return size

    async def _try_external(
        self,
        peer,
        source: Source,
        caption: str,
        size: int | None,
        progress: TransferProgress | None,
        quality: Quality,
    ) -> TelegramFile | None:
        if progress is not None:
            progress.phase = "external"
            progress.method = "external"
        trace = current_transfer.get()
        if trace is not None:
            trace.method = "external"
        count("external_attempts")
        try:
            external = types.InputMediaDocumentExternal(source.url)
            if (
                progress is not None
                or self.max_file_bytes
                or size is not None
                or streamable_video(quality)
            ):
                # Fetch first without publishing a message. Cancelling this wait
                # prevents the later send, even if Telegram finishes its own fetch.
                fetched = await self._fetch_external(peer, external)
                document = getattr(fetched, "document", None)
                if not document:
                    raise DownloadError("تلگرام فایل قابل استفاده مجدد برنگرداند.")
                actual_size = getattr(document, "size", None)
                if self.max_file_bytes and actual_size is None:
                    raise DownloadError("اندازه فایل دریافت‌شده توسط تلگرام مشخص نیست.")
                check_file_size(actual_size, self.max_file_bytes)
                if size is not None and actual_size != size:
                    raise ExternalFileMismatch
                if streamable_video(quality) and document_streaming(document) is not True:
                    raise ExternalVideoNotStreamable
                external = types.InputMediaDocument(
                    types.InputDocument(
                        document.id,
                        document.access_hash,
                        document.file_reference,
                    )
                )
            message = await self._send_file(
                peer,
                external,
                caption=caption,
                parse_mode=None,
                progress=progress,
            )
        except (
            *EXTERNAL_FAILURES,
            ExternalFetchTimeout,
            ExternalFileMismatch,
            ExternalVideoNotStreamable,
        ) as error:
            count("external_fallbacks")
            if trace is not None:
                trace.clear_errors()
                trace.telemetry.emit(
                    "external_fallback", **trace.fields(), reason=type(error).__name__
                )
            message = None
        if message is not None:
            return capture_file(
                message, peer, video_hint=True if streamable_video(quality) else None
            )
        return None

    async def _stream_file(
        self,
        peer,
        media: Media,
        quality: Quality,
        source: Source,
        caption: str,
        progress: TransferProgress | None,
    ) -> TelegramFile:
        name = re.sub(r'[\\/:*?"<>|]', "_", media.title)[:100]
        filename = f"{name}-{media.content_id}-{quality.key}.{quality.extension}"
        if (trace := current_transfer.get()) is not None:
            trace.method = source.protocol
        if progress is not None:
            progress.phase = "streaming"
            progress.method = source.protocol
            progress.duration = media.duration
            if source.protocol in {"hls", "dash"} and quality.bitrate and media.duration:
                # This is an estimate until remuxing ends and the exact output size is known.
                progress.estimated_total = quality.bitrate * 1000 * media.duration // 8
        counters = progress or TransferProgress()
        handle, info = await self._upload(source, quality, filename, counters)
        # Explicit typed media avoids Telethon's filesystem checks and sync metadata readers.
        attributes = [types.DocumentAttributeFilename(filename)]
        if quality.mime_type.startswith("audio/"):
            attributes.append(
                types.DocumentAttributeAudio(
                    duration=media.duration,
                    title=media.title,
                    performer=media.artist,
                )
            )
        elif info is not None:
            attributes.append(
                types.DocumentAttributeVideo(
                    duration=counters.seconds or info.duration or media.duration,
                    w=info.width,
                    h=info.height,
                    supports_streaming=True,
                    nosound=not info.has_audio,
                    preload_prefix_size=info.prefix_size,
                    video_codec=info.codec,
                )
            )
        elif quality.mime_type.startswith("video/") and quality.width and quality.height:
            attributes.append(
                types.DocumentAttributeVideo(
                    duration=media.duration,
                    w=quality.width,
                    h=quality.height,
                    supports_streaming=False,
                )
            )
        uploaded = types.InputMediaUploadedDocument(
            file=handle,
            mime_type=quality.mime_type,
            attributes=attributes,
            force_file=True if quality.mime_type.startswith("image/") else None,
            nosound_video=True if quality.mime_type.startswith("video/") else None,
        )
        message = await self._send_file(
            peer,
            uploaded,
            caption=caption,
            parse_mode=None,
            progress=progress,
        )
        return capture_file(message, peer, video_hint=True if info is not None else None)

    async def _upload(self, source, quality, filename, progress):
        async def upload(chunks):
            return await upload_stream(
                self._upload_part,
                chunks,
                filename,
                progress=progress,
                parallelism=self.upload_parallelism,
                max_file_bytes=self.max_file_bytes,
                upload_slots=self._upload_slots,
            )

        video = streamable_video(quality)
        if video and source.protocol == "progressive":
            async with inspect_mp4(
                media_chunks(self.http, source, quality, self.ffmpeg, progress),
                self.max_file_bytes,
            ) as (chunks, info):
                if info is not None:
                    return await upload(chunks), info
            # Close the first response before remuxing a source with its index at the end.
            progress.downloaded = 0
            progress.download_done = False
            progress.total = None
        remux = source.protocol in {"hls", "dash"} or video
        if remux:
            progress.phase = "waiting_process"
        async with acquired(self._remux_slots, "remux_wait") if remux else nullcontext():
            progress.phase = "streaming"
            chunks = media_chunks(
                self.http,
                source,
                quality,
                self.ffmpeg,
                progress,
                remux_progressive=video,
            )
            if video:
                async with inspect_mp4(chunks, self.max_file_bytes) as (stream, info):
                    info = require_video_info(info)
                    return await upload(stream), info
            return await upload(chunks), None

    async def prepare_cached(self, file: TelegramFile, quality: Quality) -> TelegramFile | None:
        if not streamable_video(quality) or file.video_streaming is True:
            return file
        if file.video_streaming is None:
            try:
                file = await self._refresh(file)
            except (DownloadError, errors.RPCError):
                return None
        return file if file.video_streaming is True else None

    async def resend(
        self,
        peer,
        file: TelegramFile,
        caption: str,
        progress: TransferProgress | None = None,
    ) -> TelegramFile:
        if self.max_file_bytes and file.size_bytes is None:
            file = await self._refresh(file)
        check_file_size(file.size_bytes, self.max_file_bytes)
        try:
            await self._send_file(
                peer,
                existing_media(file),
                caption=caption,
                parse_mode=None,
                progress=progress,
            )
            return file
        except REFERENCE_FAILURES:
            if progress is not None:
                progress.phase = "reused"
            fresh = await self._refresh(file)
            check_file_size(fresh.size_bytes, self.max_file_bytes)
            await self._send_file(
                peer,
                existing_media(fresh),
                caption=caption,
                parse_mode=None,
                progress=progress,
            )
            return fresh

    async def _refresh(self, file: TelegramFile) -> TelegramFile:
        with BinaryReader(file.origin_peer) as reader:
            origin = reader.tgread_object()
        message = await self._request(lambda: self.client.get_messages(origin, ids=file.message_id))
        if not message or not message.document or message.document.id != file.document_id:
            raise DownloadError("پیام اصلی فایل در تلگرام در دسترس نیست؛ مرجع قابل بازیابی نیست.")
        size = getattr(message.document, "size", None)
        if self.max_file_bytes and size is None:
            raise DownloadError("اندازه فایل ذخیره‌شده مشخص نیست؛ امکان ارسال وجود ندارد.")
        return replace(
            file,
            access_hash=message.document.access_hash,
            file_reference=message.document.file_reference,
            size_bytes=size,
            video_streaming=document_streaming(message.document, file.video_streaming),
        )


def existing_media(file: TelegramFile):
    return types.InputMediaDocument(
        types.InputDocument(
            file.document_id,
            file.access_hash,
            file.file_reference,
        )
    )


def document_streaming(document, fallback=None) -> bool | None:
    attributes = getattr(document, "attributes", None)
    if attributes is None:
        return fallback
    return any(
        isinstance(item, types.DocumentAttributeVideo)
        and item.supports_streaming
        and item.w > 0
        and item.h > 0
        for item in attributes
    )


def capture_file(message, peer, video_hint=None) -> TelegramFile:
    if not message.document:
        raise DownloadError("تلگرام فایل قابل استفاده مجدد برنگرداند.")
    document = message.document
    return TelegramFile(
        document.id,
        document.access_hash,
        document.file_reference,
        bytes(peer),
        message.id,
        getattr(document, "size", None),
        document_streaming(document, video_hint),
    )
