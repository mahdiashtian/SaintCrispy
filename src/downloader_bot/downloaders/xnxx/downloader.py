import asyncio
from collections.abc import Coroutine
from dataclasses import replace
from typing import Any

from downloader_bot.downloaders.base import Downloader
from downloader_bot.models import DownloadError, Media, Quality, SiteHTTPError, Source

from .client import XNXXClient
from .parser import HLSVariant, hls_duration, read_hls_variants, read_video, video_id

Playable = tuple[Quality, Source, int]


class XNXXDownloader(Downloader):
    """Inspect playable formats and refresh only the selected format at delivery."""

    def __init__(self, client: XNXXClient):
        self.client = client

    def cache_key(self, url: str) -> str:
        return video_id(url)

    async def _page(self, url: str) -> Media:
        video_id(url)
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
        final_sources = set()
        unique = []
        for item in playable:
            quality, source, _ = item
            if source.protocol == "progressive":
                if source.url in final_sources:
                    continue
                final_sources.add(source.url)
            unique.append(item)
        playable = unique
        if not playable:
            raise DownloadError(
                "هیچ کیفیت قابل دریافت و کاملی برای این ویدیو پیدا نشد.",
                code="xnxx_no_playable_format",
            )
        return replace(
            media,
            qualities=tuple(quality for quality, _, _ in playable),
            duration=media.duration or max(seconds for _, _, seconds in playable),
        )

    async def resolve(self, media: Media, quality: Quality) -> Source:
        if media.site != "xnxx" or quality not in media.qualities:
            raise DownloadError("کیفیت انتخاب‌شده متعلق به این ویدیو نیست.")
        # Refresh signed URLs and check identity before requesting any new CDN source.
        fresh = await self._page(media.page_url)
        if fresh.content_id != media.content_id:
            raise DownloadError("صفحه ویدیو تغییر کرده است؛ لینک را دوباره بفرست.")
        fresh = replace(fresh, duration=fresh.duration or media.duration)
        for candidate in fresh.qualities:
            if candidate.key != quality.key and not (
                candidate.protocol == "hls" and quality.key.startswith("hls_")
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
        results = await asyncio.gather(*tasks, return_exceptions=True)
        playable = []
        for result in results:
            if isinstance(result, SiteHTTPError) and result.status == 429:
                raise result
            if isinstance(result, DownloadError):
                continue
            if isinstance(result, BaseException):
                raise result
            playable.extend(result)
        return playable

    async def _sources(
        self,
        media: Media,
        quality: Quality,
        selected_key: str | None = None,
    ) -> list[Playable]:
        headers = self.client.source_headers(media.page_url)
        if quality.protocol == "progressive":
            probe = await self.client.mp4(quality.endpoint, media.page_url)
            if probe is None:
                return []
            return [
                (
                    replace(quality, endpoint=probe.url),
                    Source(probe.url, "progressive", headers, size_bytes=probe.size_bytes),
                    media.duration,
                )
            ]
        manifest, final_url = await self.client.manifest(quality.endpoint, media.page_url)
        variants = read_hls_variants(manifest, final_url)
        if variants:
            return await self._collect(
                [
                    self._variant(media, variant)
                    for variant in variants
                    if selected_key is None or variant.quality().key == selected_key
                ]
            )
        if selected_key is not None and selected_key != quality.key:
            return []
        seconds = hls_duration(manifest, final_url, media.duration)
        return [
            (
                replace(quality, endpoint=final_url, duration=seconds),
                Source(final_url, "hls", headers, duration=seconds),
                seconds,
            )
        ]

    async def _variant(self, media: Media, variant: HLSVariant) -> list[Playable]:
        manifest, final_url = await self.client.manifest(variant.url, media.page_url)
        seconds = hls_duration(manifest, final_url, media.duration)
        audio_url = None
        if variant.audio_url:
            audio_manifest, audio_url = await self.client.manifest(
                variant.audio_url, media.page_url
            )
            audio_seconds = hls_duration(audio_manifest, audio_url, media.duration or seconds)
            if abs(audio_seconds - seconds) > max(3, seconds * 0.05):
                raise DownloadError("مدت تصویر و صدای HLS یکسان نیست.")
        return [
            (
                replace(variant.quality(), endpoint=final_url, duration=seconds),
                Source(
                    final_url,
                    "hls",
                    self.client.source_headers(media.page_url),
                    audio_url,
                    require_audio=variant.require_audio,
                    duration=seconds,
                ),
                seconds,
            )
        ]
