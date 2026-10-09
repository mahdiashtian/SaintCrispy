"""Inspect all six origin providers, resolve real URLs, optionally register with Telegram.

Prints no credentials or signed CDN URLs. --telegram uses UploadMedia only, never
sends chat messages. This is a live origin check, separate from simulated load tests.
"""

import argparse
import asyncio
import json
import os
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from telethon import TelegramClient, functions, types
from telethon.sessions import MemorySession

from downloader_bot.downloaders.instagram.client import InstagramClient
from downloader_bot.downloaders.instagram.downloader import InstagramDownloader
from downloader_bot.downloaders.pinterest.client import PinterestClient
from downloader_bot.downloaders.pinterest.downloader import PinterestDownloader
from downloader_bot.downloaders.soundcloud.client import SoundCloudClient
from downloader_bot.downloaders.soundcloud.downloader import SoundCloudDownloader
from downloader_bot.downloaders.xnxx.client import XNXXClient
from downloader_bot.downloaders.xnxx.downloader import XNXXDownloader
from downloader_bot.downloaders.xvideos.client import XVideosClient
from downloader_bot.downloaders.xvideos.downloader import XVideosDownloader
from downloader_bot.downloaders.youtube.client import YouTubeClient
from downloader_bot.downloaders.youtube.downloader import YouTubeDownloader
from downloader_bot.models import DownloadError
from downloader_bot.streaming import media_chunks, upload_stream

SAMPLES = {
    "soundcloud": "https://soundcloud.com/gdaal/mojezeh",
    "youtube": "https://youtu.be/jNQXAC9IVRw",
    "instagram": "https://www.instagram.com/reel/Chunk8-jurw/",
    "pinterest": "https://www.pinterest.com/pin/2885187256207927/",
    "xvideos": "https://www.xvideos.com/video4588838/_",
    "xnxx": "https://www.xnxx.com/video-bykb3e9/video",
}


async def audit(args):
    async with AsyncExitStack() as stack:
        sessions = {
            site: await stack.enter_async_context(
                httpx.AsyncClient(
                    timeout=20,
                    headers={"User-Agent": "Mozilla/5.0"},
                    limits=httpx.Limits(max_connections=8),
                )
            )
            for site in SAMPLES
        }
        youtube = YouTubeClient.from_environment()
        providers = {
            "soundcloud": SoundCloudDownloader(
                SoundCloudClient(sessions["soundcloud"], os.environ.get("SOUNDCLOUD_OAUTH_TOKEN"))
            ),
            "youtube": YouTubeDownloader(youtube),
            "instagram": InstagramDownloader(
                InstagramClient(sessions["instagram"], os.environ.get("INSTAGRAM_COOKIE"))
            ),
            "pinterest": PinterestDownloader(PinterestClient(sessions["pinterest"])),
            "xvideos": XVideosDownloader(XVideosClient(sessions["xvideos"])),
            "xnxx": XNXXDownloader(XNXXClient(sessions["xnxx"])),
        }
        results = {}

        async def inspect(site):
            result = results[site] = {
                "input_url": SAMPLES[site],
                "inspected": False,
                "resolved": [],
            }
            provider = providers[site]
            try:
                async with asyncio.timeout(120):
                    media = await provider.inspect(SAMPLES[site])
                    result.update(
                        inspected=True,
                        content_id=media.content_id,
                        qualities=[
                            {
                                "key": q.key,
                                "protocol": q.protocol,
                                "width": q.width,
                                "height": q.height,
                            }
                            for q in media.qualities
                        ],
                    )
                    choices = (
                        media.qualities
                        if args.all_qualities
                        else tuple({q.protocol: q for q in reversed(media.qualities)}.values())
                    )
                    sources = []
                    for quality in choices:
                        source = await provider.resolve(media, quality)
                        row = {
                            "key": quality.key,
                            "protocol": source.protocol,
                            "cdn_host": urlsplit(source.url).hostname,
                            "separate_audio": bool(source.audio_url),
                            "size_bytes": source.size_bytes,
                            "telegram_url_candidate": source.protocol == "progressive"
                            and not source.audio_url,
                        }
                        result["resolved"].append(row)
                        async with httpx.AsyncClient(
                            proxy=source.proxy, trust_env=False, timeout=20
                        ) as http:
                            async with http.stream(
                                "GET",
                                source.url,
                                headers={
                                    **source.headers,
                                    "Range": "bytes=0-4095",
                                },
                                follow_redirects=True,
                            ) as response:
                                row["cdn_status"] = response.status_code
                                response.raise_for_status()
                                async for sample in response.aiter_bytes(4096):
                                    row["sample_bytes"] = len(sample)
                                    row["cdn_responds"] = bool(sample)
                                    break
                        if args.telegram and source.protocol == "progressive":
                            sources.append((quality, source))
                    result["_sources"] = sources
            except Exception as error:
                result.update(error_type=type(error).__name__)
                if isinstance(error, DownloadError):
                    result["reason"] = str(error)

        await asyncio.gather(*(inspect(site) for site in (args.sites or SAMPLES)))
        if args.telegram and any(row.get("_sources") for row in results.values()):
            telegram = TelegramClient(
                MemorySession(),
                int(os.environ["API_ID"]),
                os.environ["API_HASH"],
                request_retries=1,
                connection_retries=1,
                flood_sleep_threshold=0,
            )
            stack.push_async_callback(telegram.disconnect)
            try:
                async with asyncio.timeout(30):
                    await telegram.connect()
                    await telegram.sign_in(bot_token=os.environ["BOT_TOKEN"])
                for result in results.values():
                    for quality, source in result.get("_sources", []):
                        row = next(
                            item for item in result["resolved"] if item["key"] == quality.key
                        )
                        try:
                            async with asyncio.timeout(45):
                                fetched = await telegram(
                                    functions.messages.UploadMediaRequest(
                                        types.InputPeerSelf(),
                                        types.InputMediaDocumentExternal(source.url),
                                    )
                                )
                            document = fetched.document
                            row.update(
                                telegram_accepted=True,
                                telegram_bytes=document.size,
                                reusable_reference=bool(
                                    document.id and document.access_hash and document.file_reference
                                ),
                            )
                            row["size_matches_origin"] = (
                                document.size == source.size_bytes
                                if source.size_bytes is not None
                                else None
                            )
                            if args.stream_mismatches and row["size_matches_origin"] is False:
                                async with (
                                    asyncio.timeout(60),
                                    httpx.AsyncClient(timeout=20) as http,
                                ):
                                    handle = await upload_stream(
                                        telegram,
                                        media_chunks(http, source, quality, ""),
                                        f"origin-check.{quality.extension}",
                                        max_file_bytes=10 * 1024 * 1024,
                                    )
                                    streamed = await telegram(
                                        functions.messages.UploadMediaRequest(
                                            types.InputPeerSelf(),
                                            types.InputMediaUploadedDocument(
                                                handle,
                                                quality.mime_type,
                                                [
                                                    types.DocumentAttributeFilename(
                                                        f"origin-check.{quality.extension}"
                                                    )
                                                ],
                                            ),
                                        )
                                    )
                                row.update(
                                    streamed_bytes=streamed.document.size,
                                    stream_matches_origin=streamed.document.size
                                    == source.size_bytes,
                                    stream_reusable_reference=bool(
                                        streamed.document.file_reference
                                    ),
                                )
                        except Exception as error:
                            row.update(
                                telegram_accepted=False, telegram_error_type=type(error).__name__
                            )
            except Exception as error:
                for result in results.values():
                    result["telegram_login_error"] = type(error).__name__
        for result in results.values():
            result.pop("_sources", None)
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "providers": results,
            "real_telegram_messages": 0,
            "complete_media_on_disk": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites", nargs="+", choices=tuple(SAMPLES))
    parser.add_argument("--all-qualities", action="store_true")
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument(
        "--stream-mismatches",
        action="store_true",
        help="Register a byte-capped streamed upload if Telegram URL size differs; no messages",
    )
    args = parser.parse_args()
    if args.stream_mismatches and not args.telegram:
        parser.error("stream-mismatches requires telegram")
    print(json.dumps(asyncio.run(audit(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
