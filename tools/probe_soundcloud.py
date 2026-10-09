"""Inspect public playback without downloading an entire media file.

Run: python tools/probe_soundcloud.py https://soundcloud.com/gdaal/mojezeh
Only a sanitized JSON summary is printed; temporary media URLs are omitted.
"""

import asyncio
import json
import re
import sys
from urllib.parse import urljoin, urlsplit

import httpx


async def inspect_track(client: httpx.AsyncClient, url: str) -> dict:
    page = await client.get(url)
    result = {"input_url": url, "page_status": page.status_code}
    if page.status_code != 200:
        return result
    marker = "window.__sc_hydration = "
    if marker not in page.text:
        result["error"] = "No hydration metadata"
        return result
    data, _ = json.JSONDecoder().raw_decode(page.text.split(marker, 1)[1])
    track = next((item["data"] for item in data if item.get("hydratable") == "sound"), None)
    if not track:
        result["error"] = "No track metadata"
        return result
    result.update(
        {
            key: track.get(key)
            for key in (
                "id",
                "urn",
                "title",
                "duration",
                "policy",
                "streamable",
                "downloadable",
                "has_downloads_left",
                "permalink_url",
                "last_modified",
                "artwork_url",
            )
        }
    )
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', page.text)
    public_client_id = None
    for src in reversed(scripts[-8:]):
        asset = await client.get(urljoin(str(page.url), src))
        match = re.search(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"', asset.text)
        if match:
            public_client_id = match[1]
            break
    result["web_client_id_found"] = bool(public_client_id)
    if not public_client_id:
        return result
    api_track = await client.get(
        f"https://api-v2.soundcloud.com/tracks/{track['id']}",
        params={"client_id": public_client_id, "high_quality": "true"},
    )
    result["guest_high_quality_query_status"] = api_track.status_code
    if api_track.status_code == 200:
        result["guest_high_quality_query_presets"] = [
            item.get("preset") for item in api_track.json().get("media", {}).get("transcodings", [])
        ]
    params = {"client_id": public_client_id}
    if track.get("track_authorization"):
        params["track_authorization"] = track["track_authorization"]
    formats = []
    for transcoding in track.get("media", {}).get("transcodings", []):
        entry = {
            key: transcoding.get(key)
            for key in (
                "preset",
                "quality",
                "duration",
                "snipped",
                "format",
            )
        }
        response = await client.get(transcoding["url"], params=params)
        entry["resolve_status"] = response.status_code
        if response.status_code == 200:
            media_url = response.json().get("url")
            if media_url:
                entry["media_host"] = urlsplit(media_url).hostname
                async with client.stream(
                    "GET", media_url, headers={"Range": "bytes=0-1023"}
                ) as media:
                    entry["media_status"] = media.status_code
                    entry["content_type"] = media.headers.get("content-type")
                    entry["content_range"] = media.headers.get("content-range")
                    sample = b""
                    async for chunk in media.aiter_bytes():
                        sample += chunk[: 1024 - len(sample)]
                        if len(sample) >= 1024:
                            break
                    if sample.startswith(b"#EXTM3U"):
                        entry["is_hls"] = True
                        entry["hls_tags"] = [
                            line.split(":", 1)[0]
                            for line in sample.decode(errors="replace").splitlines()
                            if line.startswith("#")
                        ]
                    else:
                        entry["is_hls"] = False
                        entry["first_12_bytes_hex"] = sample[:12].hex()
        formats.append(entry)
    result["formats"] = formats
    original = await client.get(
        f"https://api-v2.soundcloud.com/tracks/{track['id']}/download",
        params={"client_id": public_client_id},
    )
    result["original_guest_status"] = original.status_code
    return result


async def main() -> None:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
    ) as client:
        for url in sys.argv[1:]:
            try:
                result = await inspect_track(client, url)
            except (httpx.HTTPError, ValueError, StopIteration) as error:
                result = {"input_url": url, "error_type": type(error).__name__}
            print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
