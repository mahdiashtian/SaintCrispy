"""Compare permitted public media transports without publishing Telegram messages.

Only the explicitly selected --relay cases transfer full media through this
process. External cases only inspect bounded HTTP samples and ask Telegram to
fetch the URL. Credentials, signed URLs and Telegram references are not printed.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import aclosing
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx
from telethon import TelegramClient, errors, functions, helpers, types
from telethon.network import (
    ConnectionTcpAbridged,
    ConnectionTcpFull,
    ConnectionTcpIntermediate,
    ConnectionTcpObfuscated,
)
from telethon.sessions import MemorySession

from downloader_bot.models import DownloadError, Quality, Source
from downloader_bot.progress import TransferProgress
from downloader_bot.streaming import PART_SIZE, media_chunks, upload_stream


@dataclass(frozen=True)
class Case:
    label: str
    url: str
    kind: str = "document"
    protocol: str = "progressive"


def cases() -> list[Case]:
    base = "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/1080/"
    small = base + "Big_Buck_Bunny_1080_10s_1MB.mp4"
    medium = base + "Big_Buck_Bunny_1080_10s_30MB.mp4"
    nonce = secrets.token_hex(8)
    image = "https://www.w3.org/Icons/w3c_home.png"
    return [
        *(
            Case(f"mp4_{size}MB", base + f"Big_Buck_Bunny_1080_10s_{size}MB.mp4")
            for size in (1, 2, 5, 10, 20, 30)
        ),
        Case("mp4_1MB_http", small.replace("https://", "http://")),
        Case("mp4_1MB_query", small + f"?probe={nonce}"),
        Case("mp4_30MB_query", medium + f"?probe={nonce}"),
        Case("mp4_20MB_query", base + f"Big_Buck_Bunny_1080_10s_20MB.mp4?probe={nonce}"),
        Case("png_query", image + f"?probe={nonce}"),
        Case("mp4_1MB_redirect", "https://httpbingo.org/redirect-to?url=" + quote(small, safe="")),
        Case(
            "mp4_30MB_redirect", "https://httpbingo.org/redirect-to?url=" + quote(medium, safe="")
        ),
        Case("sintel_mp4", "https://media.w3.org/2010/05/sintel/trailer.mp4"),
        Case("sintel_mp4_http", "http://media.w3.org/2010/05/sintel/trailer.mp4"),
        Case(
            "archive_mp4_43MB",
            "https://archive.org/download/BigBuckBunny_328/BigBuckBunny_512kb.mp4",
        ),
        Case("archive_avi_400MB", "https://archive.org/download/BigBuckBunny_328/BigBuckBunny.avi"),
        Case(
            "blender_zip_417MB",
            "https://download.blender.org/peach/bigbuckbunny_movies/big_buck_bunny_720p_h264.mov.zip",
        ),
        Case("png_document", image),
        Case("png_photo", image, kind="photo"),
        Case("pdf", "https://mozilla.github.io/pdf.js/web/compressed.tracemonkey-pldi-09.pdf"),
        Case("extensionless_bytes", "https://httpbingo.org/bytes/1024"),
        Case("html_page", "https://test-videos.co.uk/bigbuckbunny/mp4-h264"),
        Case("hls_master", "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8", protocol="hls"),
        Case(
            "hls_240",
            "https://test-streams.mux.dev/x36xhzz/url_2/193039199_mp4_h264_aac_ld_7.m3u8",
            protocol="hls",
        ),
        Case(
            "sintel_redirect",
            "https://httpbin.org/redirect-to?url=https%3A%2F%2Fmedia.w3.org%2F2010%2F05%2Fsintel%2Ftrailer.mp4",
        ),
        Case(
            "archive_jpeg_redirect",
            "https://archive.org/download/BigBuckBunny_328/__ia_thumb.jpg",
            kind="photo",
        ),
        Case(
            "webm_1MB",
            "https://test-videos.co.uk/vids/bigbuckbunny/webm/vp8/720/Big_Buck_Bunny_720_10s_1MB.webm",
        ),
        Case(
            "webm_30MB",
            "https://test-videos.co.uk/vids/bigbuckbunny/webm/vp8/720/Big_Buck_Bunny_720_10s_30MB.webm",
        ),
    ]


async def inspect(http, case: Case) -> dict:
    started = time.perf_counter()
    result = {}
    try:
        async with http.stream("GET", case.url, headers={"Range": "bytes=0-1023"}) as response:
            result.update(
                http_status=response.status_code,
                http_mime=response.headers.get("content-type"),
                last_modified=response.headers.get("last-modified"),
                final_host=urlsplit(str(response.url)).hostname,
                final_scheme=response.url.scheme,
                redirects=[r.status_code for r in response.history],
                range_accepted=response.status_code == 206,
            )
            length = response.headers.get("content-length", "")
            content_range = response.headers.get("content-range", "")
            match = re.fullmatch(r"bytes \d+-\d+/(\d+)", content_range)
            if match:
                result["source_size"] = int(match[1])
            elif response.status_code != 206 and length.isdigit():
                result["source_size"] = int(length)
            sample = bytearray()
            async for chunk in response.aiter_bytes(1024):
                sample.extend(chunk)
                if len(sample) >= 1024:
                    break
            data = bytes(sample)
            result["sample_bytes"] = len(data)
            result["source_prefix_sha256"] = hashlib.sha256(data[:1024]).hexdigest()
            result["sample_type"] = (
                "mp4"
                if b"ftyp" in data[:32]
                else "png"
                if data.startswith(b"\x89PNG")
                else "pdf"
                if data.startswith(b"%PDF")
                else "zip"
                if data.startswith(b"PK\x03\x04")
                else "avi"
                if data.startswith(b"RIFF") and b"AVI " in data[:16]
                else "hls"
                if data.startswith(b"#EXTM3U")
                else "other"
            )
    except (httpx.HTTPError, TimeoutError) as error:
        result["http_error_type"] = type(error).__name__
    result["http_seconds"] = round(time.perf_counter() - started, 3)
    return result


def reference(media):
    document = getattr(media, "document", None)
    photo = getattr(media, "photo", None)
    if document is not None:
        metadata = {
            "telegram_size": document.size,
            "telegram_mime": document.mime_type,
            "telegram_document_date": document.date.isoformat(),
            "telegram_video": any(
                isinstance(a, types.DocumentAttributeVideo) for a in document.attributes
            ),
            "telegram_video_attributes": [
                {"width": a.w, "height": a.h, "duration": a.duration}
                for a in document.attributes
                if isinstance(a, types.DocumentAttributeVideo)
            ],
        }
        return (
            types.InputMediaDocument(
                types.InputDocument(
                    document.id,
                    document.access_hash,
                    document.file_reference,
                )
            ),
            document.id,
            metadata,
        )
    if photo is not None:
        return (
            types.InputMediaPhoto(
                types.InputPhoto(
                    photo.id,
                    photo.access_hash,
                    photo.file_reference,
                )
            ),
            photo.id,
            {"telegram_photo": True},
        )
    raise ValueError("No reusable media returned")


async def verify_reference(client, media) -> dict:
    """Read at most 4 KiB from Telegram; this is not a message-send benchmark."""
    document = getattr(media, "document", None)
    photo = getattr(media, "photo", None)
    location = document
    if document is None:
        size = max(photo.sizes, key=lambda item: getattr(item, "w", 0) * getattr(item, "h", 0))
        location = types.InputPhotoFileLocation(
            photo.id,
            photo.access_hash,
            photo.file_reference,
            size.type,
        )
    started = time.perf_counter()
    iterator = client.iter_download(
        location, dc_id=(document or photo).dc_id, request_size=4096, limit=1
    )
    received = 0
    prefix = b""
    try:
        async for data in iterator:
            received += len(data)
            if not prefix:
                prefix = bytes(data[:1024])
    finally:
        await iterator.close()
    return {
        "reference_read_seconds": round(time.perf_counter() - started, 3),
        "reference_read_bytes": received,
        "reference_read_ok": received > 0,
        "telegram_prefix_sha256": hashlib.sha256(prefix).hexdigest(),
    }


async def external(client, case: Case, peer, max_seconds: float) -> dict:
    started = time.perf_counter()
    result = {}
    stage = "external_url"
    try:
        async with asyncio.timeout(max_seconds):
            media = (
                types.InputMediaPhotoExternal(case.url)
                if case.kind == "photo"
                else types.InputMediaDocumentExternal(case.url)
            )
            registered = await client(functions.messages.UploadMediaRequest(peer, media))
            result.update(
                external_accepted=True, external_seconds=round(time.perf_counter() - started, 3)
            )
            _, _, metadata = reference(registered)
            result.update(metadata)
            stage = "reference_read"
            result.update(await verify_reference(client, registered))
    except Exception as error:
        result.update(
            failed_stage=stage,
            error_type=type(error).__name__,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )
        if isinstance(error, errors.FloodWaitError):
            result["wait_seconds"] = error.seconds
    return result


async def upload_known(client, chunks, name: str, total: int, progress, parallelism: int):
    """Experimental bounded window for known-length, permitted test files."""
    file_id = helpers.generate_random_long()
    count = (total + PART_SIZE - 1) // PART_SIZE
    pending = set()
    index = received = 0
    buffer = bytearray()

    async def send(part: int, payload: bytes):
        async with asyncio.timeout(45):
            if not await client(
                functions.upload.SaveBigFilePartRequest(file_id, part, count, payload)
            ):
                raise DownloadError("Telegram did not acknowledge the part")
        progress.uploaded += len(payload)

    async def submit(payload: bytes):
        nonlocal index, pending
        if len(pending) >= parallelism:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            results = await asyncio.gather(*done, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        pending.add(asyncio.create_task(send(index, payload)))
        index += 1

    try:
        async with aclosing(chunks):
            async for chunk in chunks:
                received += len(chunk)
                if received > total:
                    raise DownloadError("Source exceeds the expected file size")
                buffer.extend(chunk)
                while len(buffer) >= PART_SIZE:
                    data = bytes(buffer[:PART_SIZE])
                    del buffer[:PART_SIZE]
                    await submit(data)
            if buffer:
                await submit(bytes(buffer))
            await asyncio.gather(*pending)
            if received != total or index != count:
                raise DownloadError("Source size differs from the expected file size")
            progress.upload_done = True
            return types.InputFileBig(file_id, count, name)
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def relay(
    client, http, case: Case, peer, ffmpeg: str, max_seconds: float, parallelism: int
) -> dict:
    started = time.perf_counter()
    progress = TransferProgress(method=case.protocol)
    stage = "transfer"
    result = {
        "local_media_download": True,
        "complete_media_on_disk": False,
        "parallelism": parallelism,
    }

    async def observe():
        while True:
            await asyncio.sleep(10)
            print(
                json.dumps(
                    {
                        "event": "relay_progress",
                        "label": case.label,
                        "downloaded_bytes": progress.downloaded,
                        "uploaded_bytes": progress.uploaded,
                    }
                ),
                flush=True,
            )

    observer = asyncio.create_task(observe())
    try:
        async with asyncio.timeout(max_seconds):
            inspected = await inspect(http, case)
            known = (
                inspected.get("source_size") if inspected.get("http_status") in {200, 206} else None
            )
            mime = (inspected.get("http_mime") or "application/octet-stream").split(";")[0]
            suffix = {
                "video/mp4": "mp4",
                "video/x-msvideo": "avi",
                "video/webm": "webm",
                "image/png": "png",
                "image/jpeg": "jpg",
                "application/pdf": "pdf",
                "application/zip": "zip",
            }.get(mime, "bin")
            if case.protocol == "hls":
                mime, suffix = "video/mp4", "mp4"
            quality = Quality("test", "test", "video", None, suffix, mime, case.protocol, case.url)
            source = Source(
                case.url,
                case.protocol,
                require_audio=case.protocol == "hls",
                duration=634.634 if case.label == "hls_240" else None,
                size_bytes=known if case.protocol == "progressive" else None,
                chunk_size=4 * 1024 * 1024
                if case.protocol == "progressive" and inspected.get("range_accepted")
                else None,
            )
            async with aclosing(media_chunks(http, source, quality, ffmpeg, progress)) as chunks:
                if parallelism > 1 and known and case.protocol == "progressive":
                    handle = await upload_known(
                        client, chunks, f"test-{case.label}.{suffix}", known, progress, parallelism
                    )
                    result["upload_method"] = "known_size_window"
                else:
                    handle = await upload_stream(
                        client, chunks, f"test-{case.label}.{suffix}", progress=progress
                    )
                    result["upload_method"] = "project_unknown_size"
            result["transfer_seconds"] = round(time.perf_counter() - started, 3)
            stage = "registration"
            registered = await client(
                functions.messages.UploadMediaRequest(
                    peer,
                    types.InputMediaUploadedDocument(
                        handle,
                        mime,
                        [types.DocumentAttributeFilename(handle.name)],
                        force_file=True if mime.startswith("image/") else None,
                    ),
                )
            )
            result.update(
                relay_accepted=True, total_seconds=round(time.perf_counter() - started, 3)
            )
            _, _, metadata = reference(registered)
            result.update(metadata)
            stage = "reference_read"
            result.update(await verify_reference(client, registered))
    except Exception as error:
        result.update(
            failed_stage=stage,
            error_type=type(error).__name__,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )
        if isinstance(error, errors.FloodWaitError):
            result["wait_seconds"] = error.seconds
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
    result.update(downloaded_bytes=progress.downloaded, uploaded_bytes=progress.uploaded)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label", action="append", default=[], help="Only these case labels; repeatable"
    )
    parser.add_argument("--peer", choices=("empty", "self", "both"), default="self")
    parser.add_argument(
        "--relay", action="append", default=[], help="Explicit case labels for full relay"
    )
    parser.add_argument("--external-seconds", type=float, default=90)
    parser.add_argument("--relay-seconds", type=float, default=1200)
    parser.add_argument("--parallelism", type=int, choices=range(1, 9), default=4)
    parser.add_argument(
        "--transport", choices=("full", "abridged", "intermediate", "obfuscated"), default="full"
    )
    parser.add_argument("--ffmpeg", default=os.environ.get("FFMPEG_PATH") or "ffmpeg")
    args = parser.parse_args()
    available = {case.label: case for case in cases()}
    unknown = set(args.label + args.relay) - available.keys()
    if unknown:
        parser.error("Unknown case labels: " + ", ".join(sorted(unknown)))
    if any(available[label].kind == "photo" for label in args.relay):
        parser.error("Photo relay is not implemented; choose a document case for that image")
    selected = [case for label, case in available.items() if not args.label or label in args.label]
    peers = ["empty", "self"] if args.peer == "both" else [args.peer]
    client = TelegramClient(
        MemorySession(),
        int(os.environ["API_ID"]),
        os.environ["API_HASH"],
        request_retries=1,
        connection_retries=1,
        flood_sleep_threshold=0,
        receive_updates=False,
        connection={
            "full": ConnectionTcpFull,
            "abridged": ConnectionTcpAbridged,
            "intermediate": ConnectionTcpIntermediate,
            "obfuscated": ConnectionTcpObfuscated,
        }[args.transport],
    )
    failed = False
    try:
        async with asyncio.timeout(60):
            await client.connect()
            await client.sign_in(bot_token=os.environ["BOT_TOKEN"])
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
        ) as http:
            for case in selected:
                inspected = await inspect(http, case)
                for mode in peers:
                    peer = types.InputPeerEmpty() if mode == "empty" else types.InputPeerSelf()
                    result = await external(client, case, peer, args.external_seconds)
                    result.update(
                        event="external_result",
                        label=case.label,
                        peer=mode,
                        method="photo_external" if case.kind == "photo" else "document_external",
                        source_host=urlsplit(case.url).hostname,
                        messages_sent=0,
                        local_media_download=False,
                        transport=args.transport,
                        **inspected,
                    )
                    if "telegram_size" in result and "source_size" in result:
                        result["source_size_matches"] = (
                            result["telegram_size"] == result["source_size"]
                        )
                    if "telegram_prefix_sha256" in result:
                        result["source_prefix_matches"] = result[
                            "telegram_prefix_sha256"
                        ] == result.get("source_prefix_sha256")
                    print(json.dumps(result), flush=True)
                    failed |= "error_type" in result
                    if result.get("error_type") == "FloodWaitError":
                        return
                    await asyncio.sleep(0.5)
            for label in args.relay:
                result = await relay(
                    client,
                    http,
                    available[label],
                    types.InputPeerSelf(),
                    args.ffmpeg,
                    args.relay_seconds,
                    args.parallelism,
                )
                result.update(
                    event="relay_result", label=label, messages_sent=0, transport=args.transport
                )
                print(json.dumps(result), flush=True)
                failed |= "error_type" in result
                if result.get("error_type") == "FloodWaitError":
                    return
    except Exception as error:
        failed = True
        result = {"event": "run_error", "error_type": type(error).__name__, "messages_sent": 0}
        if isinstance(error, errors.FloodWaitError):
            result["wait_seconds"] = error.seconds
        print(json.dumps(result), flush=True)
    finally:
        try:
            async with asyncio.timeout(10):
                await client.disconnect()
        except TimeoutError:
            pass
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
