"""Verify our direct SoundCloud/CDN extraction without an intermediary or full download.

Run with PYTHONPATH=src:
    python tools/probe_soundcloud_links.py https://soundcloud.com/gdaal/mojezeh

Signed URLs and account credentials are omitted from the JSON output.
"""

import asyncio
import json
import sys
from collections import Counter
from urllib.parse import urlsplit

import httpx

from downloader_bot.downloaders.soundcloud.client import SoundCloudClient
from downloader_bot.downloaders.soundcloud.downloader import SoundCloudDownloader
from downloader_bot.downloaders.soundcloud.parser import validate_page_url
from downloader_bot.schemas.media import DownloadError, Source


async def sample_source(http: httpx.AsyncClient, source: Source) -> dict:
    limit = 65536 if source.protocol == "hls" else 2048
    headers = source.headers.copy()
    if source.protocol == "progressive":
        headers["Range"] = f"bytes=0-{limit - 1}"
    async with http.stream("GET", source.url, headers=headers) as response:
        result = {
            "media_host": response.url.host,
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "content_range": response.headers.get("content-range"),
        }
        if not response.is_success:
            return result
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=2048):
            body.extend(chunk[: limit - len(body)])
            if len(body) >= limit:
                break
        result["sample_bytes"] = len(body)
        if source.protocol == "hls":
            manifest = body.decode("utf-8", errors="replace")
            lines = manifest.splitlines()
            result["is_hls_manifest"] = manifest.startswith("#EXTM3U")
            result["endlist"] = "#EXT-X-ENDLIST" in lines
            result["segments"] = sum(line.startswith("#EXTINF:") for line in lines)
            result["manifest_duration_seconds"] = round(
                sum(
                    float(line[8:].split(",", 1)[0])
                    for line in lines
                    if line.startswith("#EXTINF:")
                ),
                3,
            )
            result["sample_limit_reached"] = len(body) == limit
        else:
            result["first_12_bytes_hex"] = body[:12].hex()
        return result


async def probe(url: str) -> dict:
    validate_page_url(url)
    hosts = Counter()

    async def record_request(request: httpx.Request) -> None:
        hosts[request.url.host] += 1

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
        event_hooks={"request": [record_request]},
    ) as http:
        downloader = SoundCloudDownloader(SoundCloudClient(http))
        media = await downloader.inspect(url)
        formats = []
        for quality in media.qualities:
            source = await downloader.resolve(media, quality)
            entry = {
                "quality": quality.key,
                "bitrate_kbps": quality.bitrate,
                "protocol": source.protocol,
                "endpoint_host": urlsplit(quality.endpoint).hostname,
                "source_host": urlsplit(source.url).hostname,
            }
            try:
                entry.update(await sample_source(http, source))
            except httpx.HTTPError as error:
                entry["sample_error"] = type(error).__name__
            formats.append(entry)
        return {
            "track_id": media.content_id,
            "duration_seconds": media.duration,
            "account": media.account_id,
            "formats": formats,
            "request_hosts": dict(hosts),
            "full_media_saved": False,
        }


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python tools/probe_soundcloud_links.py SOUNDCLOUD_TRACK_URL")
    for url in sys.argv[1:]:
        try:
            result = await probe(url)
        except (DownloadError, httpx.HTTPError) as error:
            result = {"error_type": type(error).__name__}
        print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
