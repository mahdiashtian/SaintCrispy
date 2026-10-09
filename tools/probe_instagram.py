"""Probe Instagram/CDN and optionally Telegram UploadMedia; never send chat messages.

Example: python tools/probe_instagram.py https://www.instagram.com/reel/Chunk8-jurw/
Add --telegram to test external URL ingestion; --remux tests one highest DASH quality.
Credentials and signed URLs are omitted from output. Full media is never saved to disk.
"""

import argparse
import asyncio
import json
import os
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

import httpx
from telethon import TelegramClient, functions, types
from telethon.sessions import MemorySession

from downloader_bot.downloaders.instagram.client import InstagramClient
from downloader_bot.downloaders.instagram.downloader import InstagramDownloader
from downloader_bot.models import DownloadError
from downloader_bot.streaming import media_chunks, upload_stream


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--remux", action="store_true")
    parser.add_argument("--ffmpeg", default=os.environ.get("FFMPEG_PATH", "ffmpeg"))
    args = parser.parse_args()
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient(timeout=20))
        downloader = InstagramDownloader(InstagramClient(http, os.environ.get("INSTAGRAM_COOKIE")))
        telegram = None
        if args.telegram or args.remux:
            telegram = TelegramClient(
                MemorySession(),
                int(os.environ["API_ID"]),
                os.environ["API_HASH"],
                request_retries=1,
                connection_retries=1,
                flood_sleep_threshold=0,
            )
            stack.push_async_callback(telegram.disconnect)
            await telegram.connect()
            await telegram.sign_in(bot_token=os.environ["BOT_TOKEN"])
        failed = False
        for url in args.urls:
            try:
                async with asyncio.timeout(240):
                    media = await downloader.inspect(url)
                    print(
                        json.dumps(
                            {
                                "content_id": media.content_id,
                                "duration": media.duration,
                                "qualities": [
                                    {
                                        "key": q.key,
                                        "dimensions": [q.width, q.height],
                                        "bitrate_kbps": q.bitrate,
                                        "protocol": q.protocol,
                                        "duration": q.duration,
                                    }
                                    for q in media.qualities
                                ],
                            }
                        ),
                        flush=True,
                    )
                    direct = (
                        [q for q in media.qualities if q.protocol == "progressive"]
                        if args.telegram
                        else []
                    )
                    dash = (
                        [q for q in media.qualities if q.protocol == "dash"] if args.remux else []
                    )
                    if dash:
                        direct.append(
                            max(
                                dash,
                                key=lambda q: ((q.width or 0) * (q.height or 0), q.bitrate or 0),
                            )
                        )
                    for quality in direct:
                        source = await downloader.resolve(media, quality)
                        result = {
                            "quality": quality.key,
                            "protocol": source.protocol,
                            "cdn_host": urlsplit(source.url).hostname,
                            "separate_audio": bool(source.audio_url),
                        }
                        try:
                            if source.protocol == "progressive":
                                value = types.InputMediaDocumentExternal(source.url)
                            else:
                                handle = await upload_stream(
                                    telegram,
                                    media_chunks(http, source, quality, args.ffmpeg),
                                    f"instagram-{media.content_id}.mp4",
                                )
                                value = types.InputMediaUploadedDocument(
                                    handle,
                                    "video/mp4",
                                    [types.DocumentAttributeFilename(handle.name)],
                                )
                            received = await telegram(
                                functions.messages.UploadMediaRequest(
                                    types.InputPeerSelf(),
                                    value,
                                )
                            )
                            document = received.document
                            result.update(
                                accepted=True,
                                bytes=document.size,
                                mime_type=document.mime_type,
                                reusable_reference=bool(
                                    document.id and document.access_hash and document.file_reference
                                ),
                            )
                        except Exception as error:
                            failed = True
                            result.update(accepted=False, error_type=type(error).__name__)
                        print(json.dumps(result), flush=True)
            except Exception as error:
                failed = True
                print(
                    json.dumps(
                        {
                            "error_type": type(error).__name__,
                            "message": str(error) if isinstance(error, DownloadError) else "",
                        }
                    ),
                    flush=True,
                )
        if failed:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
