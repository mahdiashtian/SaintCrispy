import asyncio
import time
from collections import OrderedDict
from contextlib import nullcontext

from downloader_bot.core.locks import KeyedLocks
from downloader_bot.core.request_context import request_user
from downloader_bot.downloaders.base import Downloader
from downloader_bot.repositories.interfaces.media import MediaDelivery, MediaRepository
from downloader_bot.schemas.media import DownloadError, Media, Quality, TelegramFile
from downloader_bot.schemas.transfer import TransferState as TransferProgress
from downloader_bot.services.observability import (
    Telemetry,
    acquired,
    current_inspection,
    current_transfer,
    timed,
)


class DownloadService:
    def __init__(
        self,
        downloader: Downloader,
        repository: MediaRepository,
        delivery: MediaDelivery,
        storage_peer=None,
        concurrency: int = 2,
        metadata_concurrency: int = 4,
        metadata_capacity: int = 1000,
        metadata_ttl: float = 60,
        cached_concurrency: int = 4,
        transfer_timeout: float = 3600,
        telemetry: Telemetry | None = None,
    ):
        self.downloader = downloader
        self.repository = repository
        self.delivery = delivery
        self.storage_peer = storage_peer
        self._slots = asyncio.Semaphore(concurrency)
        self._locks = KeyedLocks()
        self._metadata_locks = KeyedLocks()
        self._metadata_slots = asyncio.Semaphore(metadata_concurrency)
        self._cached_slots = asyncio.Semaphore(cached_concurrency)
        self._metadata_capacity = metadata_capacity
        self._metadata_count = 0
        self._metadata_ttl = metadata_ttl
        self._transfer_timeout = transfer_timeout
        self.telemetry = telemetry
        self._metadata: OrderedDict[str, tuple[float, Media]] = OrderedDict()
        self._failures: OrderedDict[tuple[str, str], tuple[float, str, str | None]] = OrderedDict()

    def _raise_recent_failure(self, operation: str, key: str) -> None:
        cached = self._failures.get((operation, key))
        if cached is not None:
            expires, message, code = cached
            if expires > time.monotonic():
                raise DownloadError(message, code=code)
            del self._failures[operation, key]

    def _remember_failure(self, operation: str, key: str, error: Exception) -> None:
        # Concurrent followers share a short failure cooldown instead of each
        # restarting the same rejected extraction or transfer. Never retain raw
        # transport errors, which can contain credentials or signed URLs.
        message = (
            str(error)
            if isinstance(error, DownloadError)
            else "این درخواست کامل نشد؛ کمی بعد امتحان کن."
        )
        code = error.code if isinstance(error, DownloadError) else None
        self._failures[operation, key] = time.monotonic() + 10, message, code
        self._failures.move_to_end((operation, key))
        if len(self._failures) > self._metadata_capacity:
            self._failures.popitem(last=False)

    def _cached_metadata(self, url: str) -> Media | None:
        cached = self._metadata.get(url)
        if cached is None:
            return None
        expires, media = cached
        if expires <= time.monotonic():
            del self._metadata[url]
            return None
        self._metadata.move_to_end(url)
        return media

    async def inspect(self, url: str) -> Media:
        observation = self.telemetry.inspection(url) if self.telemetry else nullcontext()
        with observation as record:
            media = await self._inspect_with_history(url)
            if record is not None:
                record.update(site=media.site, quality_count=len(media.qualities))
            return media

    async def _inspect_with_history(self, url: str) -> Media:
        user_id = request_user.get()
        identity = (
            await self.repository.record_link_view(user_id, url) if user_id is not None else None
        )
        try:
            media = await self._inspect(url)
        except Exception:
            if identity is not None:
                await self.repository.finish_link_view(user_id, identity)
            raise
        if identity is not None:
            await self.repository.finish_link_view(user_id, identity, media)
        return media

    def _remember_metadata(self, key: str, media: Media) -> None:
        self._metadata[key] = time.monotonic() + self._metadata_ttl, media
        if len(self._metadata) > self._metadata_capacity:
            self._metadata.popitem(last=False)

    async def _inspect(self, url: str) -> Media:
        key = getattr(self.downloader, "cache_key", lambda value: value)(url)
        catalog_key = getattr(self.downloader, "catalog_key", lambda _: key)(url)
        observation = current_inspection.get()
        if cached := self._cached_metadata(key):
            if observation is not None:
                observation["path"] = "memory_cache"
            return cached
        if self._metadata_count >= self._metadata_capacity:
            raise DownloadError("تعداد بررسی‌های همزمان به سقف رسیده؛ کمی بعد امتحان کن.")
        self._metadata_count += 1
        try:
            # Subprocess extraction has its own resource bound, independent of file transfers.
            async with self._metadata_locks.hold(key):
                if cached := self._cached_metadata(key):
                    if observation is not None:
                        observation["path"] = "memory_cache"
                    return cached
                if self.repository is not None and catalog_key is not None:
                    if stored := await self.repository.known_media(catalog_key):
                        # This menu offers only qualities already stored in Telegram.
                        # It survives restarts and needs no new request to the origin site.
                        self._remember_metadata(key, stored)
                        if observation is not None:
                            observation["path"] = "database_catalog"
                        return stored
                self._raise_recent_failure("inspect", key)
                try:
                    if observation is not None:
                        observation["path"] = "provider"
                    async with self._metadata_slots, asyncio.timeout(90):
                        media = await self.downloader.inspect(url)
                    if self.repository is not None and catalog_key is not None:
                        canonical = getattr(self.downloader, "catalog_key", lambda value: value)(
                            media.page_url
                        )
                        aliases = (
                            (catalog_key, canonical) if canonical is not None else (catalog_key,)
                        )
                        await self.repository.remember_media(aliases, media)
                except Exception as error:
                    self._remember_failure("inspect", key, error)
                    raise
                self._remember_metadata(key, media)
                return media
        finally:
            self._metadata_count -= 1

    async def _reuse(
        self,
        peer,
        media: Media,
        quality: Quality,
        stored: TelegramFile,
        progress: TransferProgress | None,
    ) -> str:
        async with acquired(self._cached_slots, "cached_send_wait"):
            trace = current_transfer.get()
            if trace is not None:
                trace.file_size = stored.size_bytes
                if trace.method == "unknown":
                    trace.method = "reused"
            if progress is not None:
                progress.method = "reused"
                progress.phase = "reused"
            caption = f"{media.title}\n{media.artist}\n{quality.label}"
            with timed("cached_send"):
                fresh = await self.delivery.resend(peer, stored, caption, progress)
            if trace is not None:
                trace.file_size = fresh.size_bytes
            if fresh != stored:
                if progress is not None:
                    progress.phase = "saving"
                with timed("database_save"):
                    await self.repository.save(media.site, media.content_id, quality.key, fresh)
            if progress is not None:
                progress.phase = "done"
            return "reused"

    async def deliver(
        self, peer, media: Media, quality: Quality, progress: TransferProgress | None = None
    ) -> str:
        observation = (
            self.telemetry.transfer(media, quality, progress) if self.telemetry else nullcontext()
        )
        with observation as trace:
            async with asyncio.timeout(self._transfer_timeout):
                method = await self._deliver(peer, media, quality, progress)
                if trace is not None:
                    trace.result = method
                if (user_id := request_user.get()) is not None:
                    with timed("history_save"):
                        await self.repository.record_delivery(user_id, media, quality, method)
                return method

    async def _deliver(self, peer, media, quality, progress) -> str:
        key = self.repository.key(media.site, media.content_id, quality.key)
        produced = False
        with timed("cache_lookup"):
            stored = await self.repository.get(media.site, media.content_id, quality.key)
        if stored is None:
            async with acquired(self._locks.hold(key), "deduplication_wait"):
                with timed("cache_lookup"):
                    stored = await self.repository.get(media.site, media.content_id, quality.key)
                if stored is None:
                    self._raise_recent_failure("transfer", key)
                    try:
                        async with acquired(self._slots, "producer_wait"):
                            stored = await self._transfer(peer, media, quality, progress)
                            produced = True
                    except Exception as error:
                        self._remember_failure("transfer", key, error)
                        raise
        # All followers send concurrently once the one producer has persisted the file.
        # Holding the creation lock during resend would serialize a thousand recipients.
        if not produced or self.storage_peer is not None:
            await self._reuse(peer, media, quality, stored, progress)
        return "transferred" if produced else "reused"

    async def _transfer(self, peer, media, quality, progress) -> TelegramFile:
        if progress is not None:
            progress.phase = "resolving"
        async with acquired(self._metadata_slots, "resolve_wait"), asyncio.timeout(90):
            with timed("resolve"):
                if media.requires_refresh:
                    # Restarted menus store identities, never signed URLs or credentials.
                    fresh = await self.downloader.inspect(media.page_url)
                    if (fresh.site, fresh.content_id) != (media.site, media.content_id):
                        raise DownloadError("⚠️ محتوای این لینک تغییر کرده؛ لینک را دوباره بفرست.")
                    selected = next(
                        (item for item in fresh.qualities if item.key == quality.key), None
                    )
                    if selected is None or (selected.width, selected.height) != (
                        quality.width,
                        quality.height,
                    ):
                        raise DownloadError("⚠️ این کیفیت دیگر موجود نیست؛ لینک را دوباره بفرست.")
                    media, quality = fresh, selected
                source = await self.downloader.resolve(media, quality)
        target = self.storage_peer if self.storage_peer is not None else peer
        with timed("delivery"):
            reference = await self.delivery.new_file(target, media, quality, source, progress)
        if (trace := current_transfer.get()) is not None:
            trace.file_size = reference.size_bytes
        # Every successful transfer method reaches the same persistence step.
        if progress is not None:
            progress.phase = "saving"
        with timed("database_save"):
            await self.repository.save(media.site, media.content_id, quality.key, reference)
        if progress is not None:
            progress.phase = "done"
        return reference
