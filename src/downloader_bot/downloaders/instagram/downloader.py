import asyncio
import time
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import replace

from core.locks import KeyedLocks
from downloader_bot.downloaders.base import Downloader
from schemas.media import DownloadError, Media, Quality, SiteHTTPError, Source

from .client import InstagramClient
from .parser import Format, content_path, media_id, mp4_info, read_media

ACCOUNT_ATTEMPT_SECONDS = 25


class InstagramDownloader(Downloader):
    def __init__(self, client: InstagramClient, accounts: dict[str, InstagramClient] | None = None):
        self.client = client
        self.accounts = accounts or {}
        self._next_account = 0
        self._resolution_cache = OrderedDict()
        self._refresh_locks = KeyedLocks()

    def cache_key(self, url: str) -> str:
        kind, code = content_path(url)
        if kind == "post":
            return "post:" + media_id(code)
        if kind == "story":
            return "story:" + code.split("/")[1]
        return "share:" + code

    async def inspect(self, url: str) -> Media:
        try:
            async with asyncio.timeout(85):
                return await self._inspect(url)
        except TimeoutError as error:
            raise DownloadError(
                "دریافت اطلاعات اینستاگرام طولانی شد؛ کمی بعد امتحان کن.",
                code="instagram_extraction_timeout",
            ) from error

    async def _inspect(self, url: str) -> Media:
        kind, _ = content_path(url)
        clients = list(self.accounts.items())
        if clients:
            offset = self._next_account % len(clients)
            self._next_account += 1
            clients = clients[offset:] + clients[:offset]
        if kind != "story" or self.client.cookie:
            clients.insert(0, ("guest", self.client))
        error = DownloadError("دریافت استوری به نشست مجاز اینستاگرام در تنظیمات ربات نیاز دارد.")
        clients = clients[:3]
        for account_id, client in clients:
            try:
                budget = ACCOUNT_ATTEMPT_SECONDS if len(clients) > 1 else 80
                with getattr(client, "probe_batch", nullcontext)():
                    async with asyncio.timeout(budget):
                        media, formats = await self._media(client, url)
                        available = await self._available(client, media, formats)
                if not available:
                    raise DownloadError(
                        "رسانه کامل و قابل دریافت برای این پست پیدا نشد.",
                        code="instagram_no_playable_format",
                    )
                if any(fmt.quality.key.endswith("_mp4_resolver") for fmt in formats):
                    self._resolution_cache[(client, media.content_id)] = (
                        time.monotonic() + 15,
                        media,
                        formats,
                    )
                    while len(self._resolution_cache) > 128:
                        self._resolution_cache.popitem(last=False)
                return replace(
                    media, qualities=tuple(f.quality for f in available), account_id=account_id
                )
            except DownloadError as failure:
                if isinstance(failure, SiteHTTPError) and failure.status == 429:
                    raise
                error = failure
            except TimeoutError:
                error = DownloadError(
                    "دریافت اطلاعات اینستاگرام طولانی شد؛ کمی بعد امتحان کن.",
                    code="instagram_extraction_timeout",
                )
        raise error

    async def _media(self, client, url):
        node, page_url, content_id = await client.content(url)
        return read_media(node, page_url, content_id)

    async def resolve(self, media: Media, quality: Quality) -> Source:
        if media.site != "instagram" or quality not in media.qualities:
            raise DownloadError("کیفیت انتخاب‌شده معتبر نیست.")
        client = self.client if media.account_id == "guest" else self.accounts.get(media.account_id)
        if client is None:
            raise DownloadError("نشست اینستاگرام این درخواست دیگر فعال نیست؛ لینک را دوباره بفرست.")
        with getattr(client, "probe_batch", nullcontext)():
            return await self._resolve_source(client, media, quality)

    async def _resolve_source(self, client, media, quality):
        fresh, formats, cached = await self._resolution_media(client, media)
        available = await self._selected(client, fresh, formats, quality)
        if not available and cached:
            fresh, formats, _ = await self._resolution_media(client, media, refresh=True)
            available = await self._selected(client, fresh, formats, quality)
        if not available:
            raise DownloadError("کیفیت انتخاب‌شده دیگر در دسترس نیست؛ لینک را دوباره بفرست.")
        selected = available[0]
        if selected.quality.key != quality.key or (
            selected.quality.width,
            selected.quality.height,
        ) != (quality.width, quality.height):
            raise DownloadError("ابعاد کیفیت انتخاب‌شده تغییر کرده؛ لینک را دوباره بفرست.")
        return selected.source

    async def _resolution_media(self, client, media, *, refresh=False):
        # Keep one fresh snapshot for nearby quality selections in the same session.
        key = (client, media.content_id)
        async with self._refresh_locks.hold(key):
            entry = self._resolution_cache.get(key)
            if not refresh and entry and entry[0] > time.monotonic():
                self._resolution_cache.move_to_end(key)
                return entry[1], entry[2], True
            self._resolution_cache.pop(key, None)
            fresh, formats = await self._media(client, media.page_url)
            if fresh.content_id != media.content_id:
                raise DownloadError("محتوای اینستاگرام تغییر کرده؛ لینک را دوباره بفرست.")
            self._resolution_cache[key] = (time.monotonic() + 15, fresh, formats)
            while len(self._resolution_cache) > 128:
                self._resolution_cache.popitem(last=False)
            return fresh, formats, False

    async def _selected(self, client, fresh, formats, quality):
        candidates = [
            f
            for f in formats
            if f.quality.key == quality.key
            or (f.quality.codec == "video" and quality.key.startswith(f.quality.key + "_"))
        ]
        return await self._available(client, fresh, candidates)

    async def _available(self, client, media, formats):
        with getattr(client, "probe_batch", nullcontext)():
            results = await asyncio.gather(
                *(self._check(client, media, f) for f in formats), return_exceptions=True
            )
        available, failure, urls = [], None, set()
        for result in results:
            if isinstance(result, SiteHTTPError) and result.status == 429:
                raise result
            if isinstance(result, BaseException):
                if not isinstance(result, DownloadError):
                    raise result
                failure = result
            elif result and (result.source.url, result.source.audio_url) not in urls:
                urls.add((result.source.url, result.source.audio_url))
                available.append(result)
        if not available and failure:
            raise failure
        return available

    async def _check(self, client: InstagramClient, media: Media, fmt: Format) -> Format | None:
        quality, source = fmt.quality, fmt.source
        try:
            metadata = quality.protocol == "progressive" and quality.mime_type == "video/mp4"
            sample = await client.sample(source.url, media.page_url, metadata=metadata)
            if quality.mime_type == "image/jpeg":
                if not sample.startswith(b"\xff\xd8\xff"):
                    return None
            elif len(sample) < 12 or sample[4:8] != b"ftyp":
                return None
            if metadata:
                width, height, audio = mp4_info(sample)
                if width is None:
                    width, height, audio = mp4_info(
                        await client.sample(
                            source.url,
                            media.page_url,
                            metadata=True,
                            tail=True,
                        )
                    )
                if source.require_audio and audio is not True:
                    return None
                if width and height:
                    label = quality.label
                    if quality.codec == "video":
                        label = f"{quality.label.partition('MP4')[0]}MP4 · {width}×{height}"
                    key = (
                        quality.key + f"_{width}x{height}"
                        if quality.codec == "video"
                        else quality.key
                    )
                    quality = replace(quality, key=key, width=width, height=height, label=label)
            if source.audio_url:
                audio = await client.sample(source.audio_url, media.page_url)
                if len(audio) < 12 or audio[4:8] != b"ftyp":
                    return None
            return Format(
                quality,
                replace(
                    source,
                    headers=client.source_headers(media.page_url),
                    proxy=client.proxy or None,
                    **(
                        client.source_profile(source.url)
                        if source.protocol == "progressive"
                        else {}
                    ),
                ),
            )
        except SiteHTTPError as error:
            if error.status not in (401, 403, 404, 410, 416):
                raise
            return None
