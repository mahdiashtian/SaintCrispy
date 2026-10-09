"""Inspect XVideos or test Telegram ingestion without sending any messages."""

import argparse
import asyncio
import json
import os
import time
from urllib.parse import urlsplit

import httpx
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import MemorySession

from downloader_bot.bot.progress import TransferProgress
from downloader_bot.bot.transfers.streaming import media_chunks, upload_stream
from downloader_bot.downloaders.xvideos.client import XVideosClient
from downloader_bot.downloaders.xvideos.downloader import XVideosDownloader
from downloader_bot.downloaders.xvideos.urls import extract_url
from downloader_bot.schemas.media import DownloadError


async def telegram_probe(
    http, media, quality, source, *, stream: bool, ffmpeg: str, deadline_seconds: int
) -> dict:
    client = TelegramClient(
        MemorySession(),
        int(os.environ["API_ID"]),
        os.environ["API_HASH"],
        request_retries=1,
        connection_retries=1,
        flood_sleep_threshold=0,
    )
    progress = TransferProgress()
    started = time.monotonic()
    try:
        async with asyncio.timeout(deadline_seconds):
            await client.connect()
            await client.sign_in(bot_token=os.environ["BOT_TOKEN"])
            if stream:
                handle = await upload_stream(
                    client,
                    media_chunks(http, source, quality, ffmpeg, progress),
                    f"{media.content_id}-{quality.key}.{quality.extension}",
                    progress=progress,
                )
                input_media = types.InputMediaUploadedDocument(
                    file=handle,
                    mime_type=quality.mime_type,
                    attributes=[types.DocumentAttributeFilename(handle.name)],
                )
            else:
                input_media = types.InputMediaDocumentExternal(source.url)
            uploaded = await client(
                functions.messages.UploadMediaRequest(
                    peer=types.InputPeerSelf(),
                    media=input_media,
                )
            )
            document = uploaded.document
            return {
                "accepted": True,
                "mime_type": document.mime_type,
                "size": document.size,
                "reference_present": bool(document.file_reference),
                "messages_sent": 0,
                "method": "stream" if stream else "external",
                "video_downloaded_bytes": progress.downloaded,
                "video_uploaded_bytes": progress.uploaded,
                "media_seconds": round(progress.seconds, 3),
                "seconds": round(time.monotonic() - started, 2),
            }
    except errors.FloodWaitError as error:
        return {"accepted": False, "error_type": "FloodWaitError", "retry_after": error.seconds}
    except Exception as error:
        # Signed media URLs and credentials can occur in exception repr.
        return {
            "accepted": False,
            "error_type": type(error).__name__,
            "messages_sent": 0,
            "video_downloaded_bytes": progress.downloaded,
            "video_uploaded_bytes": progress.uploaded,
            "media_seconds": round(progress.seconds, 3),
            "seconds": round(time.monotonic() - started, 2),
        }
    finally:
        await client.disconnect()


async def probe(url: str, args) -> int:
    selected_key = args.quality
    async with httpx.AsyncClient(timeout=20) as http:
        downloader = XVideosDownloader(XVideosClient(http))
        try:
            async with asyncio.timeout(90):
                media = await downloader.inspect(url)
                result = {
                    "ok": True,
                    "content_id": media.content_id,
                    "page_host": urlsplit(media.page_url).hostname,
                    "duration": media.duration,
                    "qualities": [
                        {
                            "key": item.key,
                            "protocol": item.protocol,
                            "codec": item.codec,
                            "width": item.width,
                            "height": item.height,
                            "bitrate_kbps": item.bitrate,
                            "duration": item.duration,
                            "host": urlsplit(item.endpoint).hostname,
                        }
                        for item in media.qualities
                    ],
                }
                if selected_key or args.telegram or args.stream:
                    quality = next(
                        (
                            item
                            for item in media.qualities
                            if (selected_key is None or item.key == selected_key)
                            and (not args.telegram or item.protocol == "progressive")
                        ),
                        None,
                    )
                    if quality is None:
                        raise DownloadError(
                            "Select an available MP4 for --telegram or an exact quality for --stream."
                        )
                    source = await downloader.resolve(media, quality)
                    result["resolved"] = {
                        "key": quality.key,
                        "protocol": source.protocol,
                        "host": urlsplit(source.url).hostname,
                        "audio_host": urlsplit(source.audio_url).hostname
                        if source.audio_url
                        else None,
                        "size_bytes": source.size_bytes,
                        "duration": source.duration,
                        "require_audio": source.require_audio,
                    }
            if args.telegram or args.stream:
                result["telegram"] = await telegram_probe(
                    http,
                    media,
                    quality,
                    source,
                    stream=args.stream,
                    ffmpeg=args.ffmpeg,
                    deadline_seconds=args.timeout,
                )
        except (DownloadError, httpx.HTTPError, TimeoutError) as error:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": str(error)
                        if isinstance(error, DownloadError)
                        else "Network request failed.",
                    }
                )
            )
            return 1
        print(json.dumps(result, indent=2))
        return 0 if result.get("telegram", {}).get("accepted", True) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="One XVideos video, embed or Quickies URL.")
    parser.add_argument("--quality", help="Also refresh and check this quality key.")
    transfer = parser.add_mutually_exclusive_group()
    transfer.add_argument(
        "--telegram", action="store_true", help="Test Telegram fetching the MP4 URL."
    )
    transfer.add_argument(
        "--stream", action="store_true", help="Test streamed upload; remux HLS if needed."
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument(
        "--timeout", type=int, default=120, help="Telegram ingestion deadline in seconds (1–900)."
    )
    args = parser.parse_args()
    if not 1 <= args.timeout <= 900:
        parser.error("--timeout must be between 1 and 900 seconds.")
    url = extract_url(args.url)
    if not url:
        parser.error("Use an XVideos video URL.")
    return asyncio.run(probe(url, args))


if __name__ == "__main__":
    raise SystemExit(main())
