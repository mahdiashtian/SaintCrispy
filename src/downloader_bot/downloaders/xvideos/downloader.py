import asyncio
from collections.abc import Coroutine
from dataclasses import replace
from typing import Any

from downloader_bot.downloaders.base import Downloader
from downloader_bot.models import DownloadError, Media, Quality, SiteHTTPError, Source

from .client import XVideosClient
from .parser import HLSVariant, read_hls_playlist, read_hls_variants, read_video
from .urls import normalize_video_url, video_id

Playable = tuple[Quality, Source, int]


class XVideosDownloader(Downloader):
    def __init__(self, client: XVideosClient):
        self.client = client
        self.site = "xvideos"

    def cache_key(self, url: str) -> str:
        return video_id(url)

    async def _page(self, url: str) -> Media:
        url = normalize_video_url(url)
        page, final_url = await self.client.page(url)
        return read_video(page, final_url)

    async def inspect(self, url: str) -> Media:
        media = await self._page(url)
        seen = set()
        candidates = []
        for quality in media.qualities:
            if quality.endpoint not in seen:
                seen.add(quality.endpoint)
                candidates.append(quality)
        playable = await self._collect([self._sources(media, quality) for quality in candidates])
        if not playable:
            raise DownloadError(
                "هیچ کیفیت قابل دریافت و کاملی برای این ویدیو پیدا نشد.",
                code="xvideos_no_playable_format",
            )
        return replace(
            media,
            qualities=tuple(quality for quality, _, _ in playable),
            duration=media.duration or max(seconds for _, _, seconds in playable),
        )

    async def resolve(self, media: Media, quality: Quality) -> Source:
        if media.site != self.site or quality not in media.qualities:
            raise DownloadError("کیفیت انتخاب‌شده متعلق به این ویدیو نیست.")
        # Refresh signed URLs, but check identity before requesting any new CDN source.
        fresh = await self._page(media.page_url)
        if fresh.content_id != media.content_id:
            raise DownloadError("صفحه ویدیو تغییر کرده است؛ لینک را دوباره بفرست.")
        fresh = replace(fresh, duration=fresh.duration or media.duration)
        for candidate in fresh.qualities:
            if (
                candidate.key != quality.key
                and not (candidate.protocol == "hls" and quality.key.startswith("hls_"))
                and not (
                    candidate.protocol == "progressive"
                    and quality.key.startswith(candidate.key + "_")
                )
            ):
                continue
            playable = await self._collect([self._sources(fresh, candidate, quality.key)])
            for selected, source, _ in playable:
                if (
                    selected.key == quality.key
                    and selected.protocol == quality.protocol
                    and selected.codec == quality.codec
                    and (selected.width, selected.height) == (quality.width, quality.height)
                ):
                    return source
        raise DownloadError("کیفیت انتخاب‌شده دیگر در دسترس نیست؛ لینک را دوباره بفرست.")

    @staticmethod
    async def _collect(tasks: list[Coroutine[Any, Any, list[Playable]]]) -> list[Playable]:
        pending = [asyncio.create_task(task) for task in tasks]
        try:
            for completed in asyncio.as_completed(pending):
                try:
                    await completed
                except SiteHTTPError as error:
                    if error.status == 429:
                        raise
                except DownloadError:
                    pass
            # Completion order must not reorder the quality menu.
            return [item for task in pending if task.exception() is None for item in task.result()]
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _sources(
        self,
        media: Media,
        quality: Quality,
        selected_key: str | None = None,
    ) -> list[Playable]:
        headers = self.client.source_headers(media.page_url)
        if quality.protocol == "progressive":
            result = await self.client.mp4_probe(quality.endpoint, media.page_url)
            if result is None:
                return []
            source, dimensions = result
            selected = replace(quality, endpoint=source.url)
            if dimensions:
                width, height = dimensions
                selected = replace(
                    selected,
                    key=f"{quality.key}_{width}x{height}",
                    width=width,
                    height=height,
                    label=f"{quality.label} — {width}×{height}",
                )
            return [(selected, source, media.duration)]
        manifest, final_url = await self.client.manifest(quality.endpoint, media.page_url)
        variants = read_hls_variants(manifest, final_url)
        if variants:
            groups: dict[str, list[HLSVariant]] = {}
            for variant in variants:
                key = variant.quality().key
                if selected_key is None or key == selected_key:
                    if key not in groups and len(groups) >= 16:
                        continue
                    groups.setdefault(key, []).append(variant)
            return await self._collect(
                [self._alternatives(media, group[:2]) for group in groups.values()]
            )
        if selected_key is not None and selected_key != quality.key:
            return []
        seconds = await self._playlist(media, manifest, final_url)
        return [
            (
                replace(quality, endpoint=final_url, duration=int(seconds)),
                Source(final_url, "hls", headers, duration=seconds),
                int(seconds),
            )
        ]

    async def _alternatives(self, media: Media, variants: list[HLSVariant]) -> list[Playable]:
        for variant in variants:
            result = await self._collect([self._variant(media, variant)])
            if result:
                return result
        return []

    async def _playlist(self, media: Media, manifest: str, url: str) -> float:
        playlist = read_hls_playlist(manifest, url, media.duration)
        for sample in playlist.samples:
            if not await self.client.sample(sample, media.page_url):
                raise DownloadError("نمونه قطعه HLS معتبر نیست.")
        return playlist.duration

    async def _variant(self, media: Media, variant: HLSVariant) -> list[Playable]:
        manifest, final_url = await self.client.manifest(variant.url, media.page_url)
        seconds = await self._playlist(media, manifest, final_url)
        audio_url = None
        if variant.audio_url:
            audio_manifest, audio_url = await self.client.manifest(
                variant.audio_url, media.page_url
            )
            audio_seconds = await self._playlist(
                replace(media, duration=int(seconds)), audio_manifest, audio_url
            )
            if abs(audio_seconds - seconds) > max(3, seconds * 0.05):
                raise DownloadError("مدت تصویر و صدای HLS یکسان نیست.")
        return [
            (
                replace(variant.quality(), endpoint=final_url, duration=int(seconds)),
                Source(
                    final_url,
                    "hls",
                    self.client.source_headers(media.page_url),
                    audio_url,
                    require_audio=variant.require_audio,
                    duration=seconds,
                ),
                int(seconds),
            )
        ]
