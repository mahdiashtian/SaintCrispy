"""Verify public Spotify tracks and complete matched audio without Telegram access."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.progress import TransferProgress  # noqa: E402
from bot.transfers.streaming import media_chunks  # noqa: E402
from downloader_bot.downloaders.spotify.client import SpotifyClient, spotify_sessions  # noqa: E402
from downloader_bot.downloaders.spotify.downloader import SpotifyDownloader  # noqa: E402
from schemas.media import DownloadError  # noqa: E402

SAMPLES = (
    "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b",
    "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
    "https://open.spotify.com/track/7qiZfU4dY1lWllzX7mPBI3",
)


async def probe(args):
    results = []
    async with spotify_sessions(os.environ.get("SPOTIFY_PROXY", "")) as (http, api):
        provider = SpotifyDownloader(SpotifyClient.from_environment(http, api))
        for url in args.url or SAMPLES:
            row = {"metadata": False, "resolved": False, "stream_complete": False}
            started = time.monotonic()
            try:
                async with asyncio.timeout(120):
                    media = await provider.inspect(url)
                    row.update(
                        metadata=True,
                        track_id=media.content_id,
                        title=media.title,
                        artist=media.artist,
                        spotify_seconds=media.duration,
                        metadata_seconds=round(time.monotonic() - started, 4),
                    )
                    clock = time.monotonic()
                    quality = media.qualities[0]
                    source = await provider.resolve(media, quality)
                    row.update(
                        resolved=True,
                        resolve_seconds=round(time.monotonic() - clock, 4),
                        source_seconds=source.duration,
                        cdn_host=urlsplit(source.url).hostname,
                    )
                    if args.full_stream:
                        clock = time.monotonic()
                        progress, digest, total = TransferProgress(), hashlib.sha256(), 0
                        async with aclosing(
                            media_chunks(
                                http, source, quality, args.ffmpeg, progress, remux_progressive=True
                            )
                        ) as chunks:
                            async for chunk in chunks:
                                total += len(chunk)
                                if total > 64 * 1024 * 1024:
                                    raise DownloadError("Probe size limit", code="probe_size_limit")
                                digest.update(chunk)
                        if not total:
                            raise DownloadError("Empty audio", code="probe_empty_audio")
                        row.update(
                            stream_complete=True,
                            bytes=total,
                            media_seconds=progress.seconds,
                            stream_seconds=round(time.monotonic() - clock, 4),
                            sha256=digest.hexdigest(),
                        )
            except Exception as error:
                row.update(error_type=type(error).__name__, error_code=getattr(error, "code", None))
            row["elapsed_seconds"] = round(time.monotonic() - started, 4)
            results.append(row)
            print(json.dumps(row, ensure_ascii=True), flush=True)
    report = {
        "scope": "workstation_public_spotify_matched_audio_no_telegram",
        "timestamp": datetime.now(UTC).isoformat(),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return all(
        row["resolved"] and (not args.full_stream or row["stream_complete"]) for row in results
    )


def main():
    load_dotenv(ROOT / ".env", override=True, interpolate=False, encoding="utf-8-sig")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append")
    parser.add_argument("--full-stream", action="store_true")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/spotify-verification.json")
    args = parser.parse_args()
    if args.url and len(args.url) > 5:
        parser.error("At most five tracks per diagnostic")
    raise SystemExit(0 if asyncio.run(probe(args)) else 1)


if __name__ == "__main__":
    main()
