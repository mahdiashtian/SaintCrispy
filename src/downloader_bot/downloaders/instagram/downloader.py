import asyncio
from dataclasses import replace

from downloader_bot.downloaders.base import Downloader
from downloader_bot.models import DownloadError, Media, Quality, SiteHTTPError, Source

from .client import InstagramClient
from .parser import Format, content_path, media_id, mp4_info, read_media


class InstagramDownloader(Downloader):
    def __init__(self, client: InstagramClient, accounts: dict[str, InstagramClient] | None = None):
        self.client = client
        self.accounts = accounts or {}

    def cache_key(self, url: str) -> str:
        kind, code = content_path(url)
        if kind == "post":
            return "post:" + media_id(code)
        if kind == "story":
            return "story:" + code.split("/")[1]
        return "share:" + code

    async def inspect(self, url: str) -> Media:
        kind, _ = content_path(url)
        clients = list(self.accounts.items())
        if kind != "story":
            clients.insert(0, ("guest", self.client))
        error = DownloadError("دریافت استوری به نشست مجاز اینستاگرام در تنظیمات ربات نیاز دارد.")
        for account_id, client in clients:
            try:
                media, formats = await self._media(client, url)
                available = await self._available(client, media, formats)
                if not available:
                    raise DownloadError(
                        "رسانه کامل و قابل دریافت برای این پست پیدا نشد.",
                        code="instagram_no_playable_format",
                    )
                return replace(
                    media, qualities=tuple(f.quality for f in available), account_id=account_id
                )
            except DownloadError as failure:
                if isinstance(failure, SiteHTTPError) and failure.status == 429:
                    raise
                error = failure
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
        fresh, formats = await self._media(client, media.page_url)
        if fresh.content_id != media.content_id:
            raise DownloadError("محتوای اینستاگرام تغییر کرده؛ لینک را دوباره بفرست.")
        candidates = [
            f
            for f in formats
            if f.quality.key == quality.key
            or (f.quality.codec == "video" and quality.key.startswith(f.quality.key + "_"))
        ]
        available = await self._available(client, fresh, candidates)
        if not available:
            raise DownloadError("کیفیت انتخاب‌شده دیگر در دسترس نیست؛ لینک را دوباره بفرست.")
        selected = available[0]
        if selected.quality.key != quality.key or (
            selected.quality.width,
            selected.quality.height,
        ) != (quality.width, quality.height):
            raise DownloadError("ابعاد کیفیت انتخاب‌شده تغییر کرده؛ لینک را دوباره بفرست.")
        return selected.source

    async def _available(self, client, media, formats):
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
            return Format(quality, replace(source, headers=client.source_headers(media.page_url)))
        except SiteHTTPError as error:
            if error.status not in (401, 403, 404, 410, 416):
                raise
            return None
