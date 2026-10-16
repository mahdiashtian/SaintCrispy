"""Direct contract implementation with bounded metadata and source single-flight."""

import asyncio
import time
from collections import OrderedDict

import httpx

from core.locks import KeyedLocks
from downloader_bot.downloaders.base import Downloader
from schemas.media import DownloadError, SiteHTTPError

from .parser import QUALITY_KEY
from .urls import content_id, normalize_url


class SpotifyDownloader(Downloader):
    def __init__(self, client, *, capacity=128, metadata_ttl=300, source_ttl=45):
        self.client = client
        self.capacity, self.metadata_ttl, self.source_ttl = capacity, metadata_ttl, source_ttl
        self._metadata, self._sources = OrderedDict(), OrderedDict()
        self._failures = OrderedDict()
        self._locks = KeyedLocks()

    def cache_key(self, url):
        return content_id(url) or normalize_url(url)

    def catalog_key(self, url):
        return content_id(url)

    def cached(self, cache, key):
        item = cache.get(key)
        if item and item[0] > time.monotonic():
            cache.move_to_end(key)
            return item[1]
        cache.pop(key, None)
        return None

    def remember(self, cache, key, value, ttl):
        cache[key] = time.monotonic() + ttl, value
        cache.move_to_end(key)
        while len(cache) > self.capacity:
            cache.popitem(last=False)

    def recent_failure(self, key):
        if value := self.cached(self._failures, key):
            message, code, status = value
            if status:
                raise SiteHTTPError(status, message)
            raise DownloadError(message, code=code)

    def failed(self, key, error):
        self.remember(
            self._failures, key, (str(error), error.code, getattr(error, "status", None)), 2
        )

    async def inspect(self, url):
        key = self.cache_key(url)
        async with (
            asyncio.timeout(5 * self.client.timeout + 1),
            self._locks.hold("metadata:" + key),
        ):
            if media := self.cached(self._metadata, key):
                return media
            failure_key = "metadata:" + key
            self.recent_failure(failure_key)
            try:
                media = await self.client.track(normalize_url(url))
            except DownloadError as error:
                self.failed(failure_key, error)
                raise
            except (httpx.HTTPError, TimeoutError, UnicodeError, ValueError):
                error = DownloadError(
                    "دریافت اطلاعات آهنگ اسپاتیفای انجام نشد؛ کمی بعد امتحان کن.",
                    code="spotify_metadata_unavailable",
                )
                self.failed(failure_key, error)
                raise error from None
            self.remember(self._metadata, key, media, self.metadata_ttl)
            self.remember(self._metadata, media.content_id, media, self.metadata_ttl)
            return media

    async def resolve(self, media, quality):
        if (
            media.site != "spotify"
            or content_id(media.page_url) != media.content_id
            or quality not in media.qualities
            or quality.key != QUALITY_KEY
            or (quality.extension, quality.mime_type, quality.codec, quality.bitrate)
            != ("mp3", "audio/mpeg", "mp3", 128)
        ):
            raise DownloadError("کیفیت انتخاب‌شده معتبر نیست.", code="spotify_invalid_selection")
        async with (
            asyncio.timeout(self.client.search_timeout + 8 * self.client.timeout + 5),
            self._locks.hold("source:" + media.content_id),
        ):
            if source := self.cached(self._sources, media.content_id):
                return source
            failure_key = "source:" + media.content_id
            self.recent_failure(failure_key)
            try:
                fresh = await self.inspect(media.page_url)
                source = await self.client.audio(fresh)
            except DownloadError as error:
                self.failed(failure_key, error)
                raise
            except (httpx.HTTPError, TimeoutError, ValueError, OSError):
                error = DownloadError(
                    "دریافت نسخهٔ صوتی متناظر انجام نشد؛ کمی بعد امتحان کن.",
                    code="spotify_source_unavailable",
                )
                self.failed(failure_key, error)
                raise error from None
            self.remember(self._sources, media.content_id, source, self.source_ttl)
            return source
