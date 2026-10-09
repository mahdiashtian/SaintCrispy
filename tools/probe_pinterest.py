"""Probe direct Pinterest qualities and optionally Telegram ingestion, without sending messages.

Run with PYTHONPATH=src. --telegram needs API_ID, API_HASH and BOT_TOKEN.
--hls also checks one real HLS remux using FFMPEG_PATH, without a local media file.
"""

import argparse
import asyncio
import json
import os
from collections import Counter
from urllib.parse import urlsplit

import httpx
from telethon import TelegramClient, functions, types
from telethon.sessions import MemorySession

from downloader_bot.bot.transfers.streaming import media_chunks, upload_stream
from downloader_bot.downloaders.pinterest.client import PinterestClient
from downloader_bot.downloaders.pinterest.downloader import PinterestDownloader


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--hls", action="store_true")
    args = parser.parse_args()
    hosts = Counter()

    async def record(request):
        hosts[request.url.host] += 1

    def emit(value):
        print(json.dumps(value, ensure_ascii=True), flush=True)

    client = None
    try:
        if args.telegram:
            client = TelegramClient(
                MemorySession(),
                int(os.environ["API_ID"]),
                os.environ["API_HASH"],
                request_retries=1,
                connection_retries=1,
                flood_sleep_threshold=0,
            )
            await client.connect()
            await client.sign_in(bot_token=os.environ["BOT_TOKEN"])
        async with httpx.AsyncClient(timeout=30, event_hooks={"request": [record]}) as http:
            downloader = PinterestDownloader(PinterestClient(http))
            for url in args.urls:
                try:
                    async with asyncio.timeout(90):
                        media = await downloader.inspect(url)
                    emit(
                        {
                            "pin": media.content_id,
                            "formats": [
                                {
                                    "key": q.key,
                                    "label": q.label,
                                    "protocol": q.protocol,
                                    "codec": q.codec,
                                    "width": q.width,
                                    "height": q.height,
                                    "duration": q.duration,
                                    "host": urlsplit(q.endpoint).hostname,
                                }
                                for q in media.qualities
                            ],
                        }
                    )
                    if client is None:
                        continue
                    selected = []
                    seen = set()
                    for quality in media.qualities:
                        if quality.protocol != "progressive":
                            continue
                        identity = quality.codec, quality.width, quality.height
                        if identity not in seen:
                            selected.append(quality)
                            seen.add(identity)
                    for quality in selected:
                        try:
                            async with asyncio.timeout(90):
                                source = await downloader.resolve(media, quality)
                                uploaded = await client(
                                    functions.messages.UploadMediaRequest(
                                        types.InputPeerSelf(),
                                        types.InputMediaDocumentExternal(source.url),
                                    )
                                )
                                document = uploaded.document
                                emit(
                                    {
                                        "pin": media.content_id,
                                        "telegram": "external",
                                        "quality": quality.key,
                                        "accepted": True,
                                        "size": document.size,
                                        "mime": document.mime_type,
                                        "reusable_reference_present": bool(
                                            document.id
                                            and document.access_hash
                                            and document.file_reference
                                        ),
                                        "attributes": [
                                            str(attribute) for attribute in document.attributes
                                        ],
                                    }
                                )
                        except Exception as error:
                            emit(
                                {
                                    "pin": media.content_id,
                                    "telegram": "external",
                                    "quality": quality.key,
                                    "error_type": type(error).__name__,
                                }
                            )
                    if args.hls and (
                        quality := next((q for q in media.qualities if q.protocol == "hls"), None)
                    ):
                        try:
                            async with asyncio.timeout(120):
                                source = await downloader.resolve(media, quality)
                                handle = await upload_stream(
                                    client,
                                    media_chunks(
                                        http,
                                        source,
                                        quality,
                                        os.environ.get("FFMPEG_PATH") or "ffmpeg",
                                    ),
                                    "pinterest-probe.mp4",
                                )
                                uploaded = await client(
                                    functions.messages.UploadMediaRequest(
                                        types.InputPeerSelf(),
                                        types.InputMediaUploadedDocument(
                                            handle,
                                            "video/mp4",
                                            [types.DocumentAttributeFilename(handle.name)],
                                        ),
                                    )
                                )
                                emit(
                                    {
                                        "pin": media.content_id,
                                        "telegram": "hls_stream",
                                        "quality": quality.key,
                                        "accepted": True,
                                        "size": uploaded.document.size,
                                        "separate_audio": bool(source.audio_url),
                                    }
                                )
                        except Exception as error:
                            emit(
                                {
                                    "pin": media.content_id,
                                    "telegram": "hls_stream",
                                    "error_type": type(error).__name__,
                                }
                            )
                except Exception as error:
                    emit({"input_host": urlsplit(url).hostname, "error_type": type(error).__name__})
            emit({"request_hosts": dict(hosts), "full_media_saved": False, "messages_sent": False})
    finally:
        if client is not None:
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
