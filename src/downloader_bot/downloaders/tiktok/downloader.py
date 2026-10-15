"""Direct implementation of the common downloader contract for TikTok."""

import asyncio
import time
from collections import OrderedDict
from dataclasses import replace

from core.locks import KeyedLocks
from downloader_bot.downloaders.base import Downloader
from schemas.media import DownloadError, Media, Quality, Source

from .client import HEADERS, TikTokClient
from .urls import content_id, normalize_url


class TikTokDownloader(Downloader):
    def __init__(self, client: TikTokClient, *, source_ttl=45, capacity=128):
        self.client = client
        self.source_ttl, self.capacity = source_ttl, capacity
        self._catalog = OrderedDict()
        self._aliases = OrderedDict()
        self._locks = KeyedLocks()

    def cache_key(self, url: str) -> str:
        url = normalize_url(url)
        return content_id(url) or "share:" + url

    def catalog_key(self, url: str) -> str | None:
        return content_id(url)

    def _cached(self, key):
        identity = self._aliases.get(key, key)
        entry = self._catalog.get(identity)
        if entry is None:
            return None
        deadline, catalog = entry
        if deadline <= time.monotonic():
            self._catalog.pop(identity, None)
            return None
        self._catalog.move_to_end(identity)
        return catalog

    async def _content(self, url):
        key = self.cache_key(url)
        if catalog := self._cached(key):
            return catalog
        async with asyncio.timeout(2 * self.client.timeout + 1), self._locks.hold(key):
            if catalog := self._cached(key):
                return catalog
            catalog = await self.client.content(url)
            identity = catalog.media.content_id
            self._catalog[identity] = time.monotonic() + self.source_ttl, catalog
            self._catalog.move_to_end(identity)
            self._aliases[key] = identity
            self._aliases.move_to_end(key)
            while len(self._catalog) > self.capacity:
                self._catalog.popitem(last=False)
            while len(self._aliases) > self.capacity:
                self._aliases.popitem(last=False)
            return catalog

    async def inspect(self, url: str) -> Media:
        try:
            return (await self._content(normalize_url(url))).media
        except TimeoutError:
            raise DownloadError(
                "دریافت اطلاعات تیک‌تاک طولانی شد؛ دوباره امتحان کن.",
                code="tiktok_extraction_timeout",
            ) from None

    async def resolve(self, media: Media, quality: Quality) -> Source:
        if media.site != "tiktok" or quality not in media.qualities:
            raise DownloadError("کیفیت انتخاب‌شده معتبر نیست.", code="tiktok_invalid_selection")
        if content_id(media.page_url) != media.content_id:
            raise DownloadError("شناسهٔ پست تیک‌تاک معتبر نیست.", code="tiktok_identity_mismatch")
        try:
            catalog = await self._content(media.page_url)
        except TimeoutError:
            raise DownloadError(
                "دریافت فایل تیک‌تاک طولانی شد؛ دوباره امتحان کن.", code="tiktok_extraction_timeout"
            ) from None
        if catalog.media.content_id != media.content_id:
            raise DownloadError(
                "شناسهٔ پاسخ تیک‌تاک تغییر کرده است.", code="tiktok_identity_mismatch"
            )
        fresh = next((item for item in catalog.media.qualities if item.key == quality.key), None)
        if fresh is None or any(
            old is not None and old != new
            for old, new in ((quality.width, fresh.width), (quality.height, fresh.height))
        ):
            raise DownloadError(
                "این کیفیت دیگر در دسترس نیست؛ لینک را دوباره بفرست.",
                code="tiktok_quality_unavailable",
            )
        source = catalog.sources[quality.key]
        return replace(
            source, headers={**HEADERS, "Referer": media.page_url}, proxy=self.client.proxy or ""
        )
