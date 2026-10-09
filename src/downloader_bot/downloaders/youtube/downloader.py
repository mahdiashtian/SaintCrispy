from dataclasses import replace

from downloader_bot.downloaders.base import Downloader
from downloader_bot.schemas.media import DownloadError, Media, Quality, Source

from .client import YouTubeClient
from .parser import canonical_url, number, read_media, selections, video_id


class YouTubeDownloader(Downloader):
    def __init__(
        self,
        client: YouTubeClient,
        accounts: dict[str, YouTubeClient] | None = None,
    ):
        self.client = client
        self.accounts = accounts or {}
        self._next_account = 0

    def cache_key(self, url: str) -> str:
        return video_id(url)

    async def inspect(self, url: str) -> Media:
        identity = video_id(url)
        client, account_id = self.client, "guest"
        if self.accounts:
            account_id = tuple(self.accounts)[self._next_account % len(self.accounts)]
            self._next_account += 1
            client = self.accounts[account_id]
        data = await client.extract(canonical_url(identity))
        media = read_media(data, identity)
        choices = await self._available(client, data)
        if not choices:
            raise DownloadError(
                "لینک کیفیت‌های یوتیوب روی اتصال سرور قابل دریافت نیست.",
                code="youtube_cdn_unavailable",
            )
        return replace(
            media, qualities=tuple(item.quality for item in choices), account_id=account_id
        )

    async def _available(self, client, data, selected=None):
        # Validate alternatives before ranking: an inaccessible preferred URL must
        # not hide the same quality on another client or a usable original audio.
        choices = selections(data, include_alternatives=True)
        if selected:
            target = next((item for item in choices if item.quality.key == selected), None)
            if target is None:
                return ()
            audio_codec = "opus" if target.quality.codec == "vp9" else "aac"
            choices = tuple(
                item
                for item in choices
                if item.quality.key == selected
                or (
                    target.quality.mime_type.startswith("video/")
                    and item.quality.mime_type.startswith("audio/")
                    and item.quality.codec == audio_codec
                )
            )
        items = {}
        for choice in choices:
            for item in (choice.video, choice.audio):
                if item:
                    items.setdefault(item["url"], item)
        urls = await client.available(list(items.values()))
        available_data = {
            **data,
            "formats": [
                item
                for item in (data.get("formats") or [])
                if isinstance(item, dict) and item.get("url") in urls
            ],
        }
        return tuple(
            item
            for item in selections(available_data)
            if not selected or item.quality.key == selected
        )

    async def resolve(self, media: Media, quality: Quality) -> Source:
        if media.site != "youtube" or quality not in media.qualities:
            raise DownloadError("کیفیت انتخاب‌شده معتبر نیست.")
        identity = video_id(media.page_url)
        if identity != media.content_id:
            raise DownloadError("شناسه ویدیو معتبر نیست؛ لینک را دوباره بفرست.")
        try:
            client = self.client if media.account_id == "guest" else self.accounts[media.account_id]
        except KeyError:
            raise DownloadError("حساب این درخواست دیگر فعال نیست؛ لینک را دوباره بفرست.") from None
        data = await client.extract(canonical_url(identity))
        read_media(data, identity)
        for item in await self._available(client, data, quality.key):
            if item.quality.key == quality.key:
                if (item.quality.width, item.quality.height) != (quality.width, quality.height):
                    raise DownloadError("ابعاد کیفیت انتخاب‌شده تغییر کرده؛ لینک را دوباره بفرست.")
                return item.source(client.proxy, number(data.get("duration")))
        raise DownloadError("کیفیت انتخاب‌شده دیگر موجود نیست؛ لینک را دوباره بفرست.")
