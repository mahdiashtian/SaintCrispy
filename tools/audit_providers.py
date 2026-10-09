"""Inspect all six origin providers, resolve real URLs, optionally register with Telegram.

Prints no credentials or signed CDN URLs. --telegram uses UploadMedia only, never
sends chat messages. This is a live origin check, separate from simulated load tests.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from contextlib import AsyncExitStack, aclosing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
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
from downloader_bot.progress import TransferProgress
from downloader_bot.streaming import media_chunks, upload_stream
from downloader_bot.telemetry import error_fields

SAMPLES = {
    "soundcloud": "https://soundcloud.com/gdaal/mojezeh",
    "youtube": "https://youtu.be/jNQXAC9IVRw",
    "instagram": "https://www.instagram.com/reel/Chunk8-jurw/",
    "pinterest": "https://www.pinterest.com/pin/2885187256207927/",
    "xvideos": "https://www.xvideos.com/video65982001/what_s_her_name",
    "xnxx": "https://www.xnxx.com/video-55awb78/video",
}


def requested_cases(args):
    overrides = {}
    for site, url in getattr(args, "url", ()):
        overrides.setdefault(site, []).append(url)
    return [
        (site, site if index == 0 else f"{site}:{index + 1}", url)
        for site in (args.sites or SAMPLES)
        for index, url in enumerate(overrides.get(site, [SAMPLES[site]]))
    ]


def url_argument(value):
    site, separator, url = value.partition("=")
    if not separator or site not in SAMPLES or not url.startswith("https://"):
        raise argparse.ArgumentTypeError("Use SITE=https://... with one of the supported sites")
    return site, url


def failure_fields(error):
    fields = error_fields(error)
    if isinstance(error, DownloadError):
        fields["reason"] = str(error)
    return fields


async def sample_source(http, url, headers, *, progressive=True):
    options = {**headers, "Accept-Encoding": "identity"}
    if progressive:
        options["Range"] = "bytes=0-4095"
    async with http.stream("GET", url, headers=options, follow_redirects=True) as response:
        response.raise_for_status()
        async for sample in response.aiter_bytes(4096):
            return {"cdn_status": response.status_code, "sample_bytes": len(sample)}
    raise DownloadError("Origin returned an empty media body", code="audit_empty_source")


async def stream_source(http, source, quality, args, row):
    progress = TransferProgress()
    digest = hashlib.sha256()
    total = 0
    started = time.monotonic()
    row.update(stream_checked=True, stream_complete=False)
    try:
        async with aclosing(media_chunks(http, source, quality, args.ffmpeg, progress)) as chunks:
            async for chunk in chunks:
                total += len(chunk)
                if total > args.max_stream_mib * 1024 * 1024:
                    raise DownloadError("Audit byte limit reached", code="audit_byte_limit")
                digest.update(chunk)
        if not total:
            raise DownloadError("Origin returned an empty media stream", code="audit_empty_source")
        row.update(stream_complete=True, stream_sha256=digest.hexdigest())
    finally:
        row.update(
            stream_bytes=total,
            stream_seconds=round(time.monotonic() - started, 3),
            media_seconds=round(progress.seconds, 3),
        )


async def check_quality(provider, media, quality, args):
    """A broken rendition must not prevent checking the rest of the published menu."""
    row = {"key": quality.key, "protocol": quality.protocol, "verified": False}
    source = None
    try:
        async with asyncio.timeout(args.transfer_timeout):
            source = await provider.resolve(media, quality)
            row.update(
                resolved=True,
                protocol=source.protocol,
                cdn_host=urlsplit(source.url).hostname,
                separate_audio=bool(source.audio_url),
                size_bytes=source.size_bytes,
                expected_media_seconds=source.duration,
                telegram_url_candidate=source.protocol == "progressive" and not source.audio_url,
            )
            async with httpx.AsyncClient(
                proxy=source.proxy or None, trust_env=False, timeout=20
            ) as http:
                row.update(
                    await sample_source(
                        http,
                        source.url,
                        source.headers,
                        progressive=(source.input_protocol or source.protocol) != "hls",
                    )
                )
                row["cdn_responds"] = True
                if source.audio_url:
                    row["audio"] = await sample_source(
                        http,
                        source.audio_url,
                        source.audio_headers or source.headers,
                        progressive=(source.audio_protocol or source.protocol) != "hls",
                    )
                if args.stream:
                    await stream_source(http, source, quality, args, row)
            row["verified"] = True
    except Exception as error:
        row.update(failure_fields(error))
    return row, source


async def register_quality(telegram, quality, source, args, row):
    """Validate Telegram ingestion without sending a message to any chat."""
    row.update(telegram_checked=True, telegram_verified=False)
    if source.protocol == "progressive":
        try:
            async with asyncio.timeout(45):
                fetched = await telegram(
                    functions.messages.UploadMediaRequest(
                        types.InputPeerSelf(), types.InputMediaDocumentExternal(source.url)
                    )
                )
            document = fetched.document
            expected = source.size_bytes or row.get("stream_bytes")
            matched = document.size == expected if expected is not None else None
            row.update(
                telegram_external_accepted=True,
                telegram_bytes=document.size,
                size_matches_origin=matched,
            )
            if matched is not False and document.file_reference:
                row.update(telegram_verified=True, telegram_method="external")
                return
        except Exception as error:
            row.update(
                telegram_external_accepted=False, telegram_external_error=failure_fields(error)
            )
    if not args.stream and not args.stream_mismatches:
        row["telegram_error_code"] = "audit_stream_required"
        return
    try:
        progress = TransferProgress()
        async with asyncio.timeout(args.transfer_timeout), httpx.AsyncClient(timeout=20) as http:
            handle = await upload_stream(
                telegram,
                media_chunks(http, source, quality, args.ffmpeg, progress),
                f"origin-check.{quality.extension}",
                progress=progress,
                max_file_bytes=args.max_stream_mib * 1024 * 1024,
            )
            fetched = await telegram(
                functions.messages.UploadMediaRequest(
                    types.InputPeerSelf(),
                    types.InputMediaUploadedDocument(
                        handle, quality.mime_type, [types.DocumentAttributeFilename(handle.name)]
                    ),
                )
            )
        document = fetched.document
        matched = document.size == progress.downloaded
        row.update(
            telegram_method="stream",
            telegram_bytes=document.size,
            telegram_uploaded_bytes=progress.uploaded,
            telegram_downloaded_bytes=progress.downloaded,
            telegram_size_matches_stream=matched,
            telegram_verified=matched and bool(document.file_reference),
        )
    except Exception as error:
        row["telegram_stream_error"] = failure_fields(error)


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

        async def inspect(site, key, url):
            result = results[key] = {
                "site": site,
                "page_host": urlsplit(url).hostname,
                "inspected": False,
                "resolved": [],
            }
            provider = providers[site]
            try:
                async with asyncio.timeout(args.case_timeout):
                    media = await provider.inspect(url)
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
                    result["_sources"] = sources
                    result["expected_checks"] = len(choices)
                    for quality in choices:
                        row, source = await check_quality(provider, media, quality, args)
                        result["resolved"].append(row)
                        if row.get("http_status") == 429:
                            result.update(http_status=429, stopped_for_rate_limit=True)
                            break
                        if args.telegram and row["verified"]:
                            sources.append((quality, source))
            except Exception as error:
                result.update(failure_fields(error))
            result["passed"] = bool(
                result["inspected"]
                and result["resolved"]
                and len(result["resolved"]) == result.get("expected_checks")
                and all(row["verified"] for row in result["resolved"])
                and "error_type" not in result
            )

        async def inspect_site(site):
            for case in requested_cases(args):
                if case[0] == site:
                    await inspect(*case)

        await asyncio.gather(*(inspect_site(site) for site in (args.sites or SAMPLES)))
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
                async with asyncio.timeout(args.case_timeout):
                    async with asyncio.timeout(30):
                        await telegram.connect()
                        await telegram.sign_in(bot_token=os.environ["BOT_TOKEN"])
                    for result in results.values():
                        for quality, source in result.get("_sources", []):
                            row = next(
                                item for item in result["resolved"] if item["key"] == quality.key
                            )
                            await register_quality(telegram, quality, source, args, row)
            except Exception as error:
                for result in results.values():
                    result["telegram_error"] = failure_fields(error)
        for result in results.values():
            result.pop("_sources", None)
            if args.telegram:
                result["passed"] = result["passed"] and all(
                    row.get("telegram_verified", False) for row in result["resolved"]
                )
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "passed": all(row["passed"] for row in results.values()),
            "full_stream_checks": args.stream,
            "telegram_checks": args.telegram,
            "providers": results,
            "real_telegram_messages": 0,
            "complete_media_on_disk": False,
        }


def main():
    load_dotenv(
        Path(__file__).resolve().parents[1] / ".env",
        override=True,
        interpolate=False,
        encoding="utf-8-sig",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites", nargs="+", choices=tuple(SAMPLES))
    parser.add_argument(
        "--url",
        type=url_argument,
        action="append",
        default=[],
        help="Override a site's sample with SITE=https://...; repeat for multiple URLs",
    )
    parser.add_argument("--output", type=Path, help="Save the same sanitized JSON report")
    parser.add_argument("--all-qualities", action="store_true")
    parser.add_argument("--stream", action="store_true", help="Read every selected quality to EOF")
    parser.add_argument("--max-stream-mib", type=int, default=64, help="Byte cap per full stream")
    parser.add_argument("--case-timeout", type=float, default=600, help="Deadline per input URL")
    parser.add_argument("--transfer-timeout", type=float, default=180, help="Deadline per quality")
    parser.add_argument("--ffmpeg", default=os.environ.get("FFMPEG_PATH") or "ffmpeg")
    parser.add_argument(
        "--strict", action="store_true", help="Exit with failure if any check fails"
    )
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument(
        "--stream-mismatches",
        action="store_true",
        help="Register a byte-capped streamed upload if Telegram URL size differs; no messages",
    )
    args = parser.parse_args()
    if args.stream_mismatches and not args.telegram:
        parser.error("stream-mismatches requires telegram")
    if args.max_stream_mib <= 0 or args.case_timeout <= 0 or args.transfer_timeout <= 0:
        parser.error("Byte caps and deadlines must be positive")
    result = asyncio.run(audit(args))
    report = json.dumps(result, ensure_ascii=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")
    print(report)
    if args.strict and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
