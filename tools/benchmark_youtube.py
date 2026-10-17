"""Measure anonymous YouTube metadata, resolution and complete downloads; no Telegram sends."""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import time
from collections import Counter
from contextlib import aclosing
from contextvars import ContextVar
from pathlib import Path

import httpx
from dotenv import load_dotenv

from bot.progress import TransferProgress
from bot.transfers.integrity import validated_mp4
from bot.transfers.streaming import media_chunks
from downloader_bot.downloaders.youtube.client import YouTubeClient
from downloader_bot.downloaders.youtube.downloader import YouTubeDownloader
from downloader_bot.downloaders.youtube.parser import canonical_url, video_id
from schemas.media import DownloadError, SiteHTTPError

CASE_EVENTS = ContextVar("youtube_benchmark_events", default=None)


class PhaseLog(logging.Handler):
    def emit(self, record):
        events = CASE_EVENTS.get()
        if events is not None and isinstance(record.msg, dict) and len(events) < 128:
            events.append(
                {
                    key: record.msg[key]
                    for key in (
                        "event",
                        "operation",
                        "phase",
                        "http_status",
                        "error_type",
                        "error_code",
                        "available",
                        "playable",
                        "qualities",
                        "deferred_probes",
                        "network_reason",
                        "transport_code",
                        "dns_seconds",
                        "tcp_seconds",
                        "tls_seconds",
                        "first_byte_wait_seconds",
                        "elapsed_seconds",
                        "wait_seconds",
                    )
                    if key in record.msg
                }
            )


def percentile(values, fraction):
    return (
        round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 4) if values else None
    )


def safe_error(error):
    result = {"error_type": type(error).__name__, "error_code": getattr(error, "code", None)}
    cause, seen = error, set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, httpx.HTTPStatusError):
            result["http_status"] = cause.response.status_code
        elif isinstance(cause, SiteHTTPError):
            result["http_status"] = cause.status
        result["cause_type"] = type(cause).__name__
        cause = cause.__cause__
    return result


async def run(args):
    client = YouTubeClient.from_environment()
    provider = YouTubeDownloader(client)
    urls = args.url or ["https://youtu.be/jNQXAC9IVRw"]
    identities = list(dict.fromkeys(video_id(url) for url in urls))
    report = {
        "scope": "workstation_live_origin_no_telegram",
        "cold_catalog": args.cold,
        "unique_inputs": len(identities),
        "stages": [],
    }
    stopped = None
    for count in args.stages:
        start, failures, rows = time.monotonic(), 0, []
        # Stage one is deliberately sequential; later stages use the requested bound.
        slots = asyncio.Semaphore(1 if not report["stages"] else args.concurrency)

        async def case(index):
            nonlocal failures, stopped
            queued_at = time.monotonic()
            async with slots:
                identity = identities[index % len(identities)]
                row = {"index": index, "identity": identity, "success": False, "bytes": 0}
                row["queue_wait_seconds"] = round(time.monotonic() - queued_at, 4)
                if stopped:
                    return {**row, "not_attempted": stopped}
                if args.cold:
                    client._public_cache.pop(identity, None)
                    client._public_catalog.pop(identity, None)
                began = time.monotonic()
                phase = "inspect"
                progress = TransferProgress()
                events = []
                token = CASE_EVENTS.set(events)
                try:
                    async with asyncio.timeout(args.case_timeout):
                        tick = time.monotonic()
                        media = await provider.inspect(canonical_url(identity))
                        row.update(
                            inspect_seconds=round(time.monotonic() - tick, 4),
                            qualities=len(media.qualities),
                            duration=media.duration,
                        )
                        choices = sorted(
                            (q for q in media.qualities if q.mime_type.startswith("video/")),
                            key=lambda q: q.height or 10000,
                        )
                        audio = [q for q in media.qualities if q.mime_type.startswith("audio/")]
                        quality = (audio or choices)[0] if index % 2 else (choices or audio)[0]
                        row["quality"] = quality.key
                        phase, tick = "resolve", time.monotonic()
                        source = await provider.resolve(media, quality)
                        row["resolve_seconds"] = round(time.monotonic() - tick, 4)
                        phase, tick = "download", time.monotonic()
                        digest = hashlib.sha256()
                        async with httpx.AsyncClient(trust_env=False, timeout=20) as http:
                            chunks = media_chunks(
                                http,
                                source,
                                quality,
                                args.ffmpeg,
                                progress,
                                remux_progressive=quality.mime_type.startswith("audio/")
                                and quality.mime_type != "audio/mp4",
                            )
                            if quality.mime_type in {"video/mp4", "audio/mp4"}:
                                chunks = validated_mp4(
                                    chunks,
                                    progress,
                                    source.duration,
                                    source.require_audio,
                                    audio_only=quality.mime_type == "audio/mp4",
                                )
                            async with aclosing(chunks):
                                async for chunk in chunks:
                                    row["bytes"] += len(chunk)
                                    if row["bytes"] > args.max_file_mib * 1024**2:
                                        raise DownloadError(code="benchmark_byte_limit")
                                    digest.update(chunk)
                        if not row["bytes"]:
                            raise DownloadError(code="benchmark_empty_stream")
                        row.update(
                            download_seconds=round(time.monotonic() - tick, 4),
                            sha256=digest.hexdigest(),
                            media_seconds=round(progress.seconds, 3),
                            success=True,
                        )
                        failures = 0
                except Exception as error:
                    row.update(failed_phase=phase, **safe_error(error))
                    row[f"{phase}_seconds"] = round(time.monotonic() - tick, 4)
                    failures += 1
                    if row.get("http_status") == 429:
                        stopped = "upstream_rate_limit"
                    elif failures >= args.max_failures:
                        stopped = "consecutive_failures"
                finally:
                    CASE_EVENTS.reset(token)
                row["events"] = events
                row["elapsed_seconds"] = round(time.monotonic() - began, 4)
                print(json.dumps(row), flush=True)
                return row

        rows = await asyncio.gather(*(case(index) for index in range(count)))
        attempted = [row for row in rows if "not_attempted" not in row]
        stage = {
            "requested": count,
            "concurrency": 1 if not report["stages"] else args.concurrency,
            "attempted": len(attempted),
            "completed": sum(row["success"] for row in rows),
            "not_attempted": len(rows) - len(attempted),
            "elapsed_seconds": round(time.monotonic() - start, 4),
            "received_bytes": sum(row["bytes"] for row in rows),
            "elapsed_p95_seconds": percentile([row["elapsed_seconds"] for row in attempted], 0.95),
            "queue_wait_p95_seconds": percentile(
                [row["queue_wait_seconds"] for row in attempted], 0.95
            ),
            "errors": dict(
                Counter(
                    row.get("error_code") or row.get("error_type")
                    for row in attempted
                    if not row["success"]
                )
            ),
            "rows": rows,
        }
        report["stages"].append(stage)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in stage.items() if key != "rows"}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", action="append", help="Repeat for distinct inputs; samples cycle through them"
    )
    parser.add_argument(
        "--url-file", type=Path, help="UTF-8 file with one public video URL per line"
    )
    parser.add_argument("--stages", type=int, nargs="+", default=[20, 100])
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--case-timeout", type=float, default=180)
    parser.add_argument("--max-file-mib", type=int, default=256)
    parser.add_argument("--max-failures", type=int, default=3)
    parser.add_argument(
        "--cold",
        action="store_true",
        help="Clear menus/signed sources before each case; retain cooldowns",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--output", type=Path, default=Path(".runtime/youtube-batch.json"))
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 16 or any(not 1 <= count <= 1000 for count in args.stages):
        parser.error("Use 1-16 workers and 1-1000 samples per stage")
    if args.case_timeout <= 0 or args.max_file_mib < 1 or args.max_failures < 1:
        parser.error("Timeout, byte limit and failure bound must be positive")
    if args.url_file:
        args.url = (args.url or []) + [
            line.strip()
            for line in args.url_file.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    load_dotenv(
        Path(__file__).resolve().parents[1] / ".env",
        override=True,
        interpolate=False,
        encoding="utf-8-sig",
    )
    # Capture only sanitized provider phase fields, not HTTP library URL logs.
    logging.getLogger().handlers = [logging.NullHandler()]
    logging.getLogger().setLevel(logging.CRITICAL)
    logger = logging.getLogger("downloader_bot.downloaders.youtube.client")
    logger.handlers, logger.propagate = [PhaseLog()], False
    logger.setLevel(logging.INFO)
    report = asyncio.run(run(args))
    return 0 if all(stage["completed"] == stage["requested"] for stage in report["stages"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
