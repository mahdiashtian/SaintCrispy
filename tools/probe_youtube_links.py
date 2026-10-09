"""Probe our YouTube extractor/CDN and optionally Telegram UploadMedia (no messages)."""

import argparse
import asyncio
import json
import os
from contextlib import aclosing
from urllib.parse import parse_qs, urlsplit

import httpx
from telethon import TelegramClient, functions, types
from telethon.sessions import MemorySession

from downloader_bot.downloaders.youtube.client import YouTubeClient
from downloader_bot.downloaders.youtube.downloader import YouTubeDownloader
from downloader_bot.models import DownloadError
from downloader_bot.streaming import media_chunks, upload_stream
from downloader_bot.telegram import EXTERNAL_FAILURES


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--quality", help="Key from the returned qualities")
    parser.add_argument(
        "--telegram", action="store_true", help="UploadMedia only, no chat messages"
    )
    parser.add_argument("--stream", action="store_true", help="Read the entire selected quality")
    parser.add_argument("--ffmpeg", default=os.environ.get("FFMPEG_PATH") or "ffmpeg")
    args = parser.parse_args()
    client = YouTubeClient.from_environment()
    downloader = YouTubeDownloader(client)
    media = await downloader.inspect(args.url)
    print(
        json.dumps(
            {
                "id": media.content_id,
                "duration": media.duration,
                "qualities": [
                    {"key": q.key, "label": q.label, "protocol": q.protocol}
                    for q in media.qualities
                ],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if not args.quality:
        return
    quality = next((q for q in media.qualities if q.key == args.quality), None)
    if quality is None:
        raise DownloadError("کیفیت درخواستی در فهرست وجود ندارد.")
    source = await downloader.resolve(media, quality)
    print(
        json.dumps(
            {
                "protocol": source.protocol,
                "video_host": urlsplit(source.url).hostname,
                "audio_host": urlsplit(source.audio_url).hostname if source.audio_url else None,
                "url_has_ip_parameter": "ip" in parse_qs(urlsplit(source.url).query),
                "url_has_expiry": "expire" in parse_qs(urlsplit(source.url).query),
            }
        ),
        flush=True,
    )
    async with httpx.AsyncClient(proxy=client.proxy or None, trust_env=False, timeout=30) as http:
        for label, url, headers in (
            ("media", source.url, source.headers),
            ("audio", source.audio_url, source.audio_headers),
        ):
            if not url:
                continue
            async with http.stream(
                "GET", url, headers={**headers, "Range": "bytes=0-4095"}
            ) as result:
                sample = b""
                async for chunk in result.aiter_bytes(4096):
                    sample = chunk
                    break
                print(
                    json.dumps(
                        {
                            "source": label,
                            "status": result.status_code,
                            "content_type": result.headers.get("content-type"),
                            "sample_bytes": len(sample),
                            "mp4_header": b"ftyp" in sample[:32],
                            "hls_header": sample.startswith(b"#EXTM3U"),
                        }
                    ),
                    flush=True,
                )
                result.raise_for_status()
        if args.telegram:
            telegram = TelegramClient(
                MemorySession(),
                int(os.environ["API_ID"]),
                os.environ["API_HASH"],
                request_retries=1,
                connection_retries=1,
                flood_sleep_threshold=0,
            )
            try:
                await telegram.connect()
                await telegram.sign_in(bot_token=os.environ["BOT_TOKEN"])
                if source.protocol == "progressive":
                    try:
                        result = await telegram(
                            functions.messages.UploadMediaRequest(
                                types.InputPeerSelf(),
                                types.InputMediaDocumentExternal(source.url),
                            )
                        )
                        print(
                            json.dumps({"telegram_external": True, "bytes": result.document.size}),
                            flush=True,
                        )
                        return
                    except EXTERNAL_FAILURES as error:
                        print(
                            json.dumps(
                                {"telegram_external": False, "error_type": type(error).__name__}
                            ),
                            flush=True,
                        )
                if args.stream:
                    handle = await upload_stream(
                        telegram,
                        media_chunks(http, source, quality, args.ffmpeg),
                        f"youtube-{media.content_id}.{quality.extension}",
                    )
                    result = await telegram(
                        functions.messages.UploadMediaRequest(
                            types.InputPeerSelf(),
                            types.InputMediaUploadedDocument(
                                handle,
                                quality.mime_type,
                                [types.DocumentAttributeFilename(handle.name)],
                            ),
                        )
                    )
                    print(
                        json.dumps({"telegram_stream": True, "bytes": result.document.size}),
                        flush=True,
                    )
            finally:
                await telegram.disconnect()
        elif args.stream:
            count = 0
            async with aclosing(media_chunks(http, source, quality, args.ffmpeg)) as chunks:
                async for chunk in chunks:
                    count += len(chunk)
            print(json.dumps({"complete_stream_bytes": count}), flush=True)


async def safe_main():
    try:
        async with asyncio.timeout(600):
            await main()
    except DownloadError as error:
        print(
            json.dumps(
                {"error_type": type(error).__name__, "message": str(error)}, ensure_ascii=False
            )
        )
        raise SystemExit(1) from None
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(safe_main())
