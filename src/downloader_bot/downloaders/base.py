from abc import ABC, abstractmethod

from downloader_bot.models import Media, Quality, Source


class Downloader(ABC):
    def cache_key(self, url: str) -> str:
        """Provider-owned stable identity, or an alias requiring later resolution."""
        return url

    def catalog_key(self, url: str) -> str | None:
        """A stable URL identity safe for reuse after process restarts."""
        return self.cache_key(url)

    @abstractmethod
    async def inspect(self, url: str) -> Media:
        """Identify content and list the available, complete qualities."""

    @abstractmethod
    async def resolve(self, media: Media, quality: Quality) -> Source:
        """Get a fresh delivery URL for the selected quality."""
