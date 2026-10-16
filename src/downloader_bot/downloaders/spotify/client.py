"""Owned async Spotify sessions and bounded matched-audio extraction workers."""

import asyncio
import logging
import math
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from urllib.parse import urljoin

import httpx

from core.bounded_curl import BoundedSession
from core.config import ConfigurationError
from core.source_http import CurlTransport
from schemas.media import DownloadError, SiteHTTPError, Source

from .parser import (
    conversion_url,
    is_mp3,
    match_recording,
    music_recordings,
    read_object,
    read_track,
)
from .urls import DEFAULT_RESOLVER, RESOLVER_ORIGIN, content_id, normalize_url, resolver_url

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
log = logging.getLogger(__name__)


@asynccontextmanager
async def spotify_sessions(proxy=""):
    async with httpx.AsyncClient(
        trust_env=False,
        proxy=proxy or None,
        timeout=8,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    ) as http:
        async with BoundedSession(
            impersonate="chrome", discard_cookies=True, trust_env=False, proxy=proxy or None
        ) as browser:
            async with httpx.AsyncClient(
                transport=CurlTransport(browser),
                trust_env=False,
                headers={"Origin": RESOLVER_ORIGIN, "Referer": RESOLVER_ORIGIN + "/"},
            ) as api:
                api.headers.pop("User-Agent", None)
                yield http, api


class SpotifyClient:
    def __init__(
        self, http, api, *, proxy="", endpoint=DEFAULT_RESOLVER, timeout=8, search_timeout=20
    ):
        try:
            self.endpoint = resolver_url(endpoint)
            if any(
                type(v) not in (float, int) or not math.isfinite(v)
                for v in (timeout, search_timeout)
            ):
                raise ValueError()
            if not 1 <= timeout <= 20 or not 5 <= search_timeout <= 45:
                raise ValueError()
        except (ValueError, DownloadError):
            raise ConfigurationError(
                "Invalid Spotify configuration",
                code="invalid_value",
                fields=(
                    "SPOTIFY_RESOLVER_URL",
                    "SPOTIFY_METADATA_TIMEOUT_SECONDS",
                    "SPOTIFY_SEARCH_TIMEOUT_SECONDS",
                ),
            ) from None
        self.http, self.api, self.proxy = http, api, proxy
        self.timeout, self.search_timeout = timeout, search_timeout
        self._slots = asyncio.Semaphore(2)
        self._retry_at = {}

    @classmethod
    def from_environment(cls, http, api):
        try:
            timeout = float(os.environ.get("SPOTIFY_METADATA_TIMEOUT_SECONDS") or 8)
            search_timeout = float(os.environ.get("SPOTIFY_SEARCH_TIMEOUT_SECONDS") or 20)
        except ValueError:
            raise ConfigurationError(
                "Invalid Spotify timeout",
                code="invalid_number",
                fields=("SPOTIFY_METADATA_TIMEOUT_SECONDS", "SPOTIFY_SEARCH_TIMEOUT_SECONDS"),
            ) from None
        return cls(
            http,
            api,
            proxy=os.environ.get("SPOTIFY_PROXY", ""),
            endpoint=os.environ.get("SPOTIFY_RESOLVER_URL", DEFAULT_RESOLVER),
            timeout=timeout,
            search_timeout=search_timeout,
        )

    def check_rate(self, scope):
        if self._retry_at.get(scope, 0) > time.monotonic():
            raise SiteHTTPError(429, "مسیر اسپاتیفای موقتاً محدود شده؛ کمی بعد امتحان کن.")

    def rate_limit(self, scope):
        self._retry_at[scope] = time.monotonic() + 60
        raise SiteHTTPError(429, "مسیر اسپاتیفای موقتاً محدود شده؛ کمی بعد امتحان کن.")

    async def request(self, http, scope, method, url, *, limit=1024 * 1024, **options):
        self.check_rate(scope)
        started = time.monotonic()
        async with http.stream(
            method, url, timeout=self.timeout, follow_redirects=False, **options
        ) as response:
            if response.status_code == 429:
                self.rate_limit(scope)
            if response.status_code >= 400:
                raise DownloadError(
                    "دریافت اطلاعات اسپاتیفای یا مسیر صوتی انجام نشد.",
                    code="spotify_origin_rejected",
                )
            body = bytearray()
            async for chunk in response.aiter_bytes(65536):
                body.extend(chunk)
                if len(body) > limit:
                    raise DownloadError(
                        "پاسخ مسیر اسپاتیفای معتبر نیست.", code="spotify_response_too_large"
                    )
            log.info(
                {
                    "event": "spotify_request_complete",
                    "scope": scope,
                    "method": method,
                    "http_status": response.status_code,
                    "elapsed_seconds": round(time.monotonic() - started, 4),
                }
            )
            return response, bytes(body)

    async def track(self, url):
        current = normalize_url(url)
        for _ in range(4):
            if identity := content_id(current):
                break
            response, _ = await self.request(self.http, "spotify", "GET", current, headers=HEADERS)
            if response.status_code not in {301, 302, 303, 307, 308} or not response.headers.get(
                "location"
            ):
                raise DownloadError(
                    "لینک کوتاه اسپاتیفای به آهنگ نرسید؛ لینک کامل آهنگ رو بفرست.",
                    code="spotify_short_link_failed",
                )
            current = normalize_url(urljoin(current, response.headers["location"]))
        else:
            raise DownloadError(
                "تعداد تغییر مسیر لینک اسپاتیفای زیاد بود.", code="spotify_short_link_failed"
            )
        _, body = await self.request(
            self.http,
            "spotify",
            "GET",
            f"https://open.spotify.com/embed/track/{identity}",
            headers=HEADERS,
        )
        return read_track(body.decode("utf-8"), identity)

    async def search(self, media):
        self.check_rate("search")
        queries = (
            f"ytsearch10:{media.artist} {media.title} official audio",
            f"ytsearch10:{media.artist} {media.title}",
        )
        # Prefer the songs catalog before broad video queries displace radio edits.
        # Every search shares the original deadline and the same strict matcher.
        async with asyncio.timeout(self.search_timeout):
            try:
                return await self._music_search(media)
            except SiteHTTPError:
                raise
            except (DownloadError, httpx.HTTPError) as error:
                log.info(
                    {
                        "event": "spotify_music_search_unavailable",
                        "track_id": media.content_id,
                        "error_code": getattr(error, "code", "spotify_music_connection_failed"),
                    }
                )
            for index, query in enumerate(queries):
                try:
                    return await self._search_query(media, query)
                except DownloadError as error:
                    if error.code != "spotify_no_matching_audio" or index == len(queries) - 1:
                        raise
                    log.info(
                        {
                            "event": "spotify_search_no_match",
                            "track_id": media.content_id,
                            "query_index": index,
                        }
                    )

    async def _music_search(self, media):
        # The pinned extractor's flat Music results omit artist and duration.
        # Read the songs shelf directly; never infer them from the query or album.
        _, body = await self.request(
            self.http,
            "search",
            "POST",
            "https://music.youtube.com/youtubei/v1/search",
            headers={
                **HEADERS,
                "Origin": "https://music.youtube.com",
                "Referer": "https://music.youtube.com/",
            },
            json={
                "context": {
                    "client": {
                        "clientName": "WEB_REMIX",
                        "clientVersion": "1.20260707.12.00",
                        "hl": "en",
                        "gl": "US",
                    }
                },
                "query": f"{media.artist} {media.title}",
                "params": "EgWKAQIIAWoKEAoQAxAEEAkQBQ==",
            },
        )
        result = match_recording(music_recordings(read_object(body)), media)
        log.info(
            {
                "event": "spotify_recording_matched",
                "search_kind": "music_songs",
                "track_id": media.content_id,
                "video_id": result[0],
                "spotify_seconds": media.duration,
                "source_seconds": result[1],
            }
        )
        return result

    async def _search_query(self, media, query):
        arguments = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-cache-dir",
            "--no-plugin-dirs",
            "--no-remote-components",
            "--skip-download",
            "--flat-playlist",
            "--no-warnings",
            "--dump-single-json",
            "--socket-timeout",
            "6",
            "--retries",
            "0",
            "--extractor-retries",
            "0",
            "--proxy",
            self.proxy,
            "--",
            query,
        ]
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def bounded(stream, limit):
            result = bytearray()
            while chunk := await stream.read(8192):
                result.extend(chunk)
                if len(result) > limit:
                    raise DownloadError(
                        "پاسخ جست‌وجوی آهنگ بیش از حد بزرگ بود.", code="spotify_search_failed"
                    )
            return bytes(result)

        readers = [
            asyncio.create_task(bounded(process.stdout, 512 * 1024)),
            asyncio.create_task(bounded(process.stderr, 65536)),
        ]
        try:
            async with asyncio.timeout(self.search_timeout):
                output, stderr = await asyncio.gather(*readers)
                await process.wait()
            if process.returncode:
                if b"429" in stderr:
                    self.rate_limit("search")
                raise DownloadError(
                    "جست‌وجوی نسخهٔ متناظر آهنگ انجام نشد.", code="spotify_search_failed"
                )
            identity, duration = match_recording(read_object(output), media)
            log.info(
                {
                    "event": "spotify_recording_matched",
                    "track_id": media.content_id,
                    "video_id": identity,
                    "spotify_seconds": media.duration,
                    "source_seconds": duration,
                }
            )
            return identity, duration
        finally:
            if process.returncode is None:
                process.kill()
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            async with asyncio.timeout(5):
                await process.communicate()

    async def audio(self, media):
        if not self.endpoint:
            raise DownloadError(
                "مسیر صوتی اسپاتیفای توسط مدیر غیرفعال شده است.", code="spotify_resolver_disabled"
            )
        budget = self.search_timeout + 2 * self.timeout + max(15, self.timeout) + 2
        async with asyncio.timeout(budget), self._slots:
            identity, duration = await self.search(media)
            link = f"https://www.youtube.com/watch?v={identity}"
            _, body = await self.request(
                self.api,
                "converter",
                "GET",
                self.endpoint + "/v2/sanity/key",
                params={"link": link},
            )
            key = read_object(body).get("key")
            if not isinstance(key, str) or not 1 <= len(key) <= 8192 or "\n" in key or "\r" in key:
                raise DownloadError("مسیر تبدیل آهنگ آماده نشد.", code="spotify_conversion_failed")
            _, body = await self.request(
                self.api,
                "converter",
                "POST",
                self.endpoint + "/v2/converter",
                headers={"key": key},
                json={
                    "link": link,
                    "format": "mp3",
                    "audioBitrate": "128",
                    "videoQuality": "360",
                    "vCodec": "h264",
                    "filenameStyle": "pretty",
                },
            )
            target = conversion_url(read_object(body), identity)
            # A short header probe rejects HTML/error bodies without buffering the song.
            self.check_rate("converter")
            async with self.http.stream(
                "GET",
                target,
                timeout=max(15, self.timeout),
                follow_redirects=False,
                headers={
                    "Referer": RESOLVER_ORIGIN + "/",
                    "Range": "bytes=0-1023",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if response.status_code == 429:
                    self.rate_limit("converter")
                if response.status_code not in {200, 206}:
                    raise DownloadError(
                        "دریافت فایل صوتی متناظر انجام نشد.", code="spotify_source_unavailable"
                    )
                sample = bytearray()
                async for chunk in response.aiter_bytes(1024):
                    sample.extend(chunk[: 1024 - len(sample)])
                    if len(sample) >= 1024:
                        break
                if not is_mp3(sample):
                    raise DownloadError(
                        "مسیر تبدیل فایل MP3 معتبر ارائه نکرد.", code="spotify_invalid_audio"
                    )
                size = None
                if response.status_code == 206:
                    match = re.fullmatch(
                        r"bytes 0-\d+/(\d{1,12})", response.headers.get("content-range", "")
                    )
                    if match:
                        size = int(match[1])
                elif (length := response.headers.get("content-length", "")).isdigit() and len(
                    length
                ) <= 12:
                    size = int(length)
            return Source(
                target,
                "progressive",
                headers={"Referer": RESOLVER_ORIGIN + "/"},
                proxy=self.proxy,
                duration=duration,
                size_bytes=size,
                size_probed=True,
                chunk_size=16 * 1024 * 1024,
            )
