"""Fetch/stream media to Telegram and reuse it, without sending anybody a message."""

import argparse
import asyncio
import json
import os
from contextlib import suppress

import httpx
from telethon import TelegramClient, functions, types
from telethon.sessions import MemorySession

from downloader_bot.bot.progress import TransferProgress
from downloader_bot.bot.transfers.streaming import media_chunks, upload_stream
from downloader_bot.downloaders.soundcloud.client import SoundCloudClient
from downloader_bot.downloaders.soundcloud.downloader import SoundCloudDownloader


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quality", default="mp3_sq")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--external-only", action="store_true")
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    client = TelegramClient(
        MemorySession(),
        int(os.environ["API_ID"]),
        os.environ["API_HASH"],
        request_retries=1,
        connection_retries=1,
        flood_sleep_threshold=0,
    )
    monitor = None
    try:
        await client.connect()
        await client.sign_in(bot_token=os.environ["BOT_TOKEN"])
        bot = await client.get_me()
        print(json.dumps({"bot": bot.username, "bot_id": bot.id}), flush=True)
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as http:
            downloader = SoundCloudDownloader(SoundCloudClient(http))
            media = await downloader.inspect("https://soundcloud.com/gdaal/mojezeh")
            quality = next(q for q in media.qualities if q.key == args.quality)
            source = await downloader.resolve(media, quality)
            progress = TransferProgress(
                phase="streaming",
                method=source.protocol,
                duration=media.duration,
                estimated_total=(quality.bitrate * 1000 * media.duration // 8)
                if quality.bitrate
                else None,
            )

            async def observe():
                while True:
                    print(json.dumps({"progress": progress.text()}), flush=True)
                    await asyncio.sleep(2.5)

            if args.progress:
                monitor = asyncio.create_task(observe())
            async with asyncio.timeout(180):
                if source.protocol == "progressive" or args.external_only:
                    progress.phase = "external"
                    input_media = types.InputMediaDocumentExternal(source.url)
                else:
                    handle = await upload_stream(
                        client,
                        media_chunks(http, source, quality, args.ffmpeg, progress),
                        f"{media.content_id}.{quality.extension}",
                        progress=progress,
                    )
                    input_media = types.InputMediaUploadedDocument(
                        file=handle,
                        mime_type=quality.mime_type,
                        attributes=[types.DocumentAttributeFilename(handle.name)],
                    )
                uploaded = await client(
                    functions.messages.UploadMediaRequest(
                        peer=types.InputPeerSelf(),
                        media=input_media,
                    )
                )
                document = uploaded.document
                if args.progress:
                    print(
                        json.dumps(
                            {
                                "downloaded_bytes": progress.downloaded,
                                "uploaded_bytes": progress.uploaded,
                                "final_size": progress.total,
                                "media_seconds": progress.seconds,
                            }
                        ),
                        flush=True,
                    )
                print(
                    json.dumps(
                        {
                            "quality": quality.key,
                            "method": source.protocol,
                            "accepted": True,
                            "size": document.size,
                            "mime_type": document.mime_type,
                        }
                    ),
                    flush=True,
                )
                print(
                    json.dumps(
                        {
                            "reusable_reference_present": bool(
                                document.id and document.access_hash and document.file_reference
                            )
                        }
                    ),
                    flush=True,
                )
    except Exception as error:
        # Avoid exception repr: request details can include credentials or signed URLs.
        print(json.dumps({"error_type": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
    finally:
        if monitor is not None:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
