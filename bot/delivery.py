import asyncio
import hashlib
import re
import time
from contextlib import aclosing, nullcontext
from dataclasses import replace

import httpx
from telethon import TelegramClient, errors, functions, helpers, types, utils
from telethon.extensions import BinaryReader

from bot.cards.preview import MAX_PREVIEW_INPUT, video_thumbnail
from bot.premium_emoji import BotPresentation
from bot.progress import TransferProgress
from bot.texts.downloads import download_caption
from bot.transfers.faststart import prepared_video
from bot.transfers.integrity import require_duration, validated_mp4
from bot.transfers.streaming import media_chunks, source_size, upload_stream
from bot.transfers.video import (
    MAX_HEADER_BYTES,
    MAX_PREPARED_HEADER_BYTES,
    inspect_mp4,
    require_video_info,
)
from core.request_context import request_chat, request_message
from schemas.media import (
    DownloadError,
    Media,
    Quality,
    Source,
    TelegramFile,
    streamable_video,
)
from services.limits import check_file_size
from services.observability import acquired, count, current_transfer, timed

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


class ExternalMediaIncomplete(Exception):
    """Telegram registered a preview or could not establish the full duration."""


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
        presentation: BotPresentation | None = None,
        bot_username: str | None = None,
    ):
        self.client = client
        self.http = http
        self.ffmpeg = ffmpeg
        self.upload_parallelism = upload_parallelism
        self.max_file_bytes = max_file_bytes
        self.external_timeout = external_timeout
        self.presentation = presentation if presentation is not None else BotPresentation()
        self.bot_username = bot_username
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
                lambda: self.presentation.send_file(self.client, peer, media, **options),
                publish=True,
                progress=progress,
            )
        count("telegram_publications")
        return result

    async def _upload_part(self, request):
        # Part RPCs share the same explicit Telegram cooldown as file publication.
        async def send():
            prefix = (
                "preview_upload"
                if isinstance(request, functions.upload.SaveFilePartRequest)
                else "upload"
            )
            count(f"{prefix}_part_attempts")
            count(f"{prefix}_attempt_bytes", len(request.bytes))
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
        if quality.mime_type.startswith(("audio/", "video/")):
            source = replace(source, duration=source.duration or media.duration or None)
        caption = download_caption(media, quality, self.bot_username)
        if (trace := current_transfer.get()) is not None:
            trace.protocol = source.protocol
            trace.source_size = source.size_bytes
            trace.expected_media_seconds = source.duration
        with timed("size_probe"):
            size = await self._checked_source_size(source)
        if trace is not None:
            trace.source_size = size
        if size is not None and source.protocol == "progressive":
            source = replace(source, size_bytes=size)
        # Unknown-size sources use the byte-capped stream instead of unrestricted URL fetching.
        if source.protocol == "progressive" and (not self.max_file_bytes or size is not None):
            reference = await self._try_external(peer, source, caption, size, progress, quality)
            if reference is not None:
                return reference
        return await self._stream_file(peer, media, quality, source, caption, progress)

    async def _checked_source_size(self, source: Source) -> int | None:
        check_file_size(source.size_bytes, self.max_file_bytes)
        size = source.size_bytes
        if (
            source.protocol == "progressive"
            and self.max_file_bytes
            and size is None
            and not source.size_probed
        ):
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
                or source.duration
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
                if source.duration:
                    kind = (
                        types.DocumentAttributeVideo
                        if quality.mime_type.startswith("video/")
                        else types.DocumentAttributeAudio
                    )
                    duration = next(
                        (
                            a.duration
                            for a in (getattr(document, "attributes", None) or ())
                            if isinstance(a, kind)
                        ),
                        None,
                    )
                    try:
                        require_duration(duration, source.duration)
                    except DownloadError as error:
                        raise ExternalMediaIncomplete from error
                    if trace is not None:
                        trace.received_media_seconds = duration
                if streamable_video(quality) and (
                    document_streaming(document) is not True
                    or document_preview(document) is not True
                ):
                    record_video_document(document)
                    raise ExternalVideoNotStreamable
                if streamable_video(quality):
                    record_video_document(document)
                    try:
                        indexed = await self._telegram_faststart(document)
                    except (errors.RPCError, TimeoutError, OSError, ValueError) as error:
                        raise ExternalVideoNotStreamable from error
                    if not indexed:
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
            ExternalMediaIncomplete,
        ) as error:
            count("external_fallbacks")
            if trace is not None:
                trace.clear_errors()
                trace.telemetry.emit(
                    "external_fallback", **trace.fields(), reason=type(error).__name__
                )
            message = None
        if message is not None:
            if trace is not None:
                trace.completeness_verified = True
            return capture_file(
                message,
                peer,
                video_hint=True if streamable_video(quality) else None,
                verified_complete=True,
                video_faststart=True if streamable_video(quality) else None,
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
        prefix = bytearray() if streamable_video(quality) else None
        handle, info = await self._upload(
            source, quality, filename, counters, preview_prefix=prefix
        )
        if (trace := current_transfer.get()) is not None:
            trace.received_media_seconds = counters.seconds or None
            trace.completeness_verified = True
        # Explicit typed media avoids Telethon's filesystem checks and sync metadata readers.
        attributes = [types.DocumentAttributeFilename(filename)]
        if quality.mime_type.startswith("audio/"):
            attributes.append(
                types.DocumentAttributeAudio(
                    duration=round(counters.seconds or media.duration),
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
        if info is not None:
            # A video flag alone does not provide a usable preview in Telegram clients.
            # Reuse bytes already transferred; never fetch or store the whole video again.
            async with acquired(self._remux_slots, "preview_wait"):
                with timed("preview_prepare"):
                    jpeg = await video_thumbnail(self.ffmpeg, bytes(prefix))
            prefix.clear()
            identity = helpers.generate_random_long()
            async with self._upload_slots:
                async with asyncio.timeout(45):
                    accepted = await self._upload_part(
                        functions.upload.SaveFilePartRequest(identity, 0, jpeg)
                    )
            if not accepted:
                raise DownloadError("آپلود پیش‌نمایش ویدیو ناموفق بود.")
            count("preview_upload_acked_bytes", len(jpeg))
            uploaded.thumb = types.InputFile(
                identity, 1, "preview.jpg", hashlib.md5(jpeg).hexdigest()
            )
            # Verify the server's document before publishing. A missing attribute or
            # thumbnail must not be silently certified from our requested attributes.
            registered = await self._request(
                lambda: self.client(functions.messages.UploadMediaRequest(peer, uploaded))
            )
            document = getattr(registered, "document", None)
            record_video_document(document)
            if document_streaming(document) is not True or document_preview(document) is not True:
                raise DownloadError(
                    "تلگرام ویدیو را برای پخش هنگام دانلود تأیید نکرد؛ فایل ارسال نشد.",
                    code="video_registration_failed",
                )
            uploaded = types.InputMediaDocument(
                types.InputDocument(document.id, document.access_hash, document.file_reference)
            )
        message = await self._send_file(
            peer,
            uploaded,
            caption=caption,
            parse_mode=None,
            progress=progress,
        )
        return capture_file(
            message,
            peer,
            video_hint=True if info is not None else None,
            verified_complete=True,
            video_faststart=info.faststart if info is not None else None,
        )

    async def _upload(self, source, quality, filename, progress, *, preview_prefix=None):
        preview_limit = MAX_PREVIEW_INPUT

        async def upload(chunks):
            async def capture():
                async with aclosing(chunks):
                    async for chunk in chunks:
                        if preview_prefix is not None and len(preview_prefix) < preview_limit:
                            take = min(len(chunk), preview_limit - len(preview_prefix))
                            preview_prefix.extend(memoryview(chunk)[:take])
                        yield chunk

            return await upload_stream(
                self._upload_part,
                capture(),
                filename,
                progress=progress,
                parallelism=self.upload_parallelism,
                max_file_bytes=self.max_file_bytes,
                upload_slots=self._upload_slots,
            )

        async def prepare(chunks, *, input_slots=None):
            nonlocal preview_limit
            checked = validated_mp4(
                chunks,
                progress,
                source.duration,
                source.require_audio,
                max_metadata_bytes=MAX_PREPARED_HEADER_BYTES,
            )
            async with prepared_video(
                checked,
                self.ffmpeg,
                progress,
                self.max_file_bytes,
                input_slots=input_slots,
                remux_slots=self._remux_slots,
            ) as prepared:
                async with inspect_mp4(
                    prepared,
                    self.max_file_bytes,
                    max_header_bytes=MAX_PREPARED_HEADER_BYTES,
                ) as (stream, info):
                    info = require_video_info(info)
                    if not info.faststart:
                        raise DownloadError("فهرست پخش ویدیو کامل نیست؛ فایل ارسال نشد.")
                    preview_limit = MAX_PREVIEW_INPUT + info.prefix_size
                    checked = validated_mp4(
                        stream,
                        progress,
                        source.duration,
                        source.require_audio,
                        max_metadata_bytes=MAX_PREPARED_HEADER_BYTES,
                    )
                    return await upload(checked), info

        video = streamable_video(quality)
        if quality.mime_type == "audio/mp4" and source.protocol == "progressive":
            chunks = media_chunks(self.http, source, quality, self.ffmpeg, progress)
            checked = validated_mp4(chunks, progress, source.duration, audio_only=True)
            return await upload(checked), None
        if video and source.protocol == "progressive":
            async with inspect_mp4(
                media_chunks(self.http, source, quality, self.ffmpeg, progress),
                self.max_file_bytes,
            ) as (chunks, info):
                if info is not None and info.faststart:
                    preview_limit = MAX_PREVIEW_INPUT + info.prefix_size
                    stream = validated_mp4(chunks, progress, source.duration, source.require_audio)
                    return await upload(stream), info
                # Keep the original response and replay the peek; never download it twice.
                return await prepare(chunks)
        if video:
            chunks = media_chunks(
                self.http,
                source,
                quality,
                self.ffmpeg,
                progress,
                remux_progressive=True,
            )
            return await prepare(chunks, input_slots=self._remux_slots)
        audio = quality.mime_type.startswith("audio/") and quality.extension in {
            "mp3",
            "m4a",
            "mp4",
            "opus",
            "ogg",
            "webm",
            "wav",
            "flac",
        }
        remux = source.protocol in {"hls", "dash"} or video or audio
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
                remux_progressive=video or audio,
            )
            return await upload(chunks), None

    async def prepare_cached(self, file: TelegramFile, quality: Quality) -> TelegramFile | None:
        # Legacy references may contain an incomplete stream with a correct-looking
        # duration attribute. Rebuild once; never certify them from attributes alone.
        if not file.verified_complete:
            return None
        if not streamable_video(quality):
            return file
        if (
            file.video_streaming is True
            and file.video_preview is True
            and file.video_faststart is True
        ):
            return file
        if file.video_streaming is None or file.video_preview is None:
            try:
                file = await self._refresh(file)
            except (DownloadError, errors.RPCError):
                return None
        if file.video_streaming is not True or file.video_preview is not True:
            return None
        if file.video_faststart is None:
            try:
                location = types.InputDocumentFileLocation(
                    file.document_id, file.access_hash, file.file_reference, ""
                )
                try:
                    indexed = await self._telegram_faststart(location)
                except REFERENCE_FAILURES:
                    file = await self._refresh(file)
                    location = types.InputDocumentFileLocation(
                        file.document_id, file.access_hash, file.file_reference, ""
                    )
                    indexed = await self._telegram_faststart(location)
                file = replace(file, video_faststart=indexed)
            except (DownloadError, errors.RPCError, TimeoutError, OSError, ValueError):
                return None
        return file if file.video_faststart is True else None

    async def _telegram_faststart(self, document) -> bool:
        # Inspect only the registered file's header, never trust a streaming flag
        # or preview as evidence of its complete sample index.
        async with asyncio.timeout(20):
            async with self.client.iter_download(
                document,
                request_size=64 * 1024,
                chunk_size=64 * 1024,
                limit=MAX_HEADER_BYTES // (64 * 1024),
            ) as chunks:

                async def read_header():
                    async for chunk in chunks:
                        yield chunk

                async with inspect_mp4(read_header()) as (_, info):
                    return info is not None and info.faststart

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
            video_preview=document_preview(message.document),
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
    if getattr(document, "mime_type", "video/mp4") != "video/mp4":
        return False
    return any(
        isinstance(item, types.DocumentAttributeVideo)
        and item.supports_streaming
        and item.w > 0
        and item.h > 0
        and item.duration > 0
        for item in attributes
    )


def document_preview(document) -> bool | None:
    thumbnails = getattr(document, "thumbs", None)
    if thumbnails is None:
        return None
    return any(getattr(item, "w", 0) > 0 and getattr(item, "h", 0) > 0 for item in thumbnails)


def record_video_document(document) -> None:
    if (trace := current_transfer.get()) is None:
        return
    video = next(
        (
            item
            for item in (getattr(document, "attributes", None) or ())
            if isinstance(item, types.DocumentAttributeVideo)
        ),
        None,
    )
    trace.telemetry.emit(
        "video_registration_checked",
        **trace.fields(),
        video_mime_type=getattr(document, "mime_type", None),
        video_width=getattr(video, "w", None),
        video_height=getattr(video, "h", None),
        video_duration_seconds=getattr(video, "duration", None),
        video_supports_streaming=document_streaming(document),
        video_preview=document_preview(document),
    )


def capture_file(
    message, peer, video_hint=None, *, verified_complete=False, video_faststart=None
) -> TelegramFile:
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
        verified_complete,
        document_preview(document),
        video_faststart,
    )
