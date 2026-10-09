"""Structural interfaces for the service's storage and delivery dependencies."""

from typing import Protocol

from downloader_bot.schemas.media import Media, Quality, Source, TelegramFile
from downloader_bot.schemas.transfer import TransferState as TransferProgress


class FileStore(Protocol):
    def key(self, site: str, content_id: str, quality: str) -> str: ...

    async def get(self, site: str, content_id: str, quality: str) -> TelegramFile | None: ...

    async def save(self, site: str, content_id: str, quality: str, file: TelegramFile) -> None: ...


class MediaCatalog(Protocol):
    async def known_media(self, alias: str) -> Media | None: ...

    async def remember_media(self, aliases: tuple[str, ...], media: Media) -> None: ...


class UserHistory(Protocol):
    async def record_link_view(self, user_id: int, url: str) -> str: ...

    async def finish_link_view(
        self, user_id: int, identity: str, media: Media | None = None
    ) -> None: ...

    async def record_delivery(
        self, user_id: int, media: Media, quality: Quality, method: str
    ) -> None: ...


class MediaRepository(FileStore, MediaCatalog, UserHistory, Protocol):
    """Storage capabilities used by DownloadService, independent of PostgreSQL/Redis."""


class MediaDelivery(Protocol):
    async def new_file(
        self,
        peer,
        media: Media,
        quality: Quality,
        source: Source,
        progress: TransferProgress | None = None,
    ) -> TelegramFile: ...

    async def resend(
        self,
        peer,
        file: TelegramFile,
        caption: str,
        progress: TransferProgress | None = None,
    ) -> TelegramFile: ...
