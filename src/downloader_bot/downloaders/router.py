from downloader_bot.models import DownloadError, Media, Quality, Source
from downloader_bot.urls import SITE_EXTRACTORS

from .base import Downloader


class DownloaderRouter(Downloader):
    def __init__(self, downloaders: dict[str, Downloader]):
        self.downloaders = downloaders

    def cache_key(self, url: str) -> str:
        site, downloader, normalized = self._route_url(url)
        return site + ":" + downloader.cache_key(normalized)

    def catalog_key(self, url: str) -> str | None:
        site, downloader, normalized = self._route_url(url)
        key = downloader.catalog_key(normalized)
        return site + ":" + key if key is not None else None

    async def inspect(self, url: str) -> Media:
        _, downloader, normalized = self._route_url(url)
        return await downloader.inspect(normalized)

    async def resolve(self, media: Media, quality: Quality) -> Source:
        return await self._downloader(media.site).resolve(media, quality)

    def _route_url(self, url: str) -> tuple[str, Downloader, str]:
        for site, extract in SITE_EXTRACTORS.items():
            if site in self.downloaders and (normalized := extract(url)):
                return site, self.downloaders[site], normalized
        raise DownloadError("سایت این لینک پشتیبانی نشده است.")

    def _downloader(self, site: str) -> Downloader:
        try:
            return self.downloaders[site]
        except KeyError:
            raise DownloadError("سایت این لینک پشتیبانی نشده است.") from None
