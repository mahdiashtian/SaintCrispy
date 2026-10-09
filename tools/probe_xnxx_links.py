"""Inspect sources or test Telegram ingestion without sending any messages."""

import argparse
import asyncio
import json
import os
import time
from urllib.parse import urlsplit

import httpx
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import MemorySession

from downloader_bot.downloaders.xnxx.client import XNXXClient
from downloader_bot.downloaders.xnxx.downloader import XNXXDownloader
from downloader_bot.models import DownloadError
from downloader_bot.progress import TransferProgress
from downloader_bot.streaming import media_chunks, upload_stream
from downloader_bot.urls import extract_xnxx_url


async def telegram_probe(http, media, quality, source, *, stream: bool, ffmpeg: str) -> dict:
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
        async with asyncio.timeout(120):
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
                "seconds": round(time.monotonic() - started, 2),
            }
    except errors.FloodWaitError as error:
        return {"accepted": False, "error_type": "FloodWaitError", "retry_after": error.seconds}
    except Exception as error:
        # Exception repr may include signed media URLs or credentials.
        return {"accepted": False, "error_type": type(error).__name__}
    finally:
        await client.disconnect()


async def probe(url: str, args) -> int:
    async with httpx.AsyncClient(timeout=20) as http:
        try:
            downloader = XNXXDownloader(XNXXClient(http))
            media = await downloader.inspect(url)
        except DownloadError as error:
            print(json.dumps({"ok": False, "error": str(error)}))
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "content_id": media.content_id,
                    "duration": media.duration,
                    "qualities": [
                        {
                            "key": quality.key,
                            "protocol": quality.protocol,
                            "width": quality.width,
                            "height": quality.height,
                            "bitrate_kbps": quality.bitrate,
                            "host": urlsplit(quality.endpoint).hostname,
                        }
                        for quality in media.qualities
                    ],
                },
                indent=2,
            )
        )
        if args.telegram or args.stream:
            quality = next(
                (
                    quality
                    for quality in media.qualities
                    if (args.quality is None or quality.key == args.quality)
                    and (args.stream or quality.protocol == "progressive")
                ),
                None,
            )
            if quality is None or (args.telegram and quality.protocol != "progressive"):
                print(json.dumps({"ok": False, "error": "Select an available MP4 for --telegram."}))
                return 1
            try:
                source = await downloader.resolve(media, quality)
                result = await telegram_probe(
                    http,
                    media,
                    quality,
                    source,
                    stream=args.stream,
                    ffmpeg=args.ffmpeg,
                )
            except Exception as error:
                result = {"accepted": False, "error_type": type(error).__name__}
            print(json.dumps({"telegram": result}))
            return 0 if result["accepted"] else 1
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="One XNXX video page URL.")
    transfer = parser.add_mutually_exclusive_group()
    transfer.add_argument(
        "--telegram", action="store_true", help="Test Telegram fetching the MP4 URL."
    )
    transfer.add_argument(
        "--stream", action="store_true", help="Test streamed upload; remux HLS if needed."
    )
    parser.add_argument("--quality", help="An exact quality key from the metadata output.")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    url = extract_xnxx_url(args.url)
    if not url:
        parser.error("Use an XNXX video page URL.")
    return asyncio.run(probe(url, args))


if __name__ == "__main__":
    raise SystemExit(main())
