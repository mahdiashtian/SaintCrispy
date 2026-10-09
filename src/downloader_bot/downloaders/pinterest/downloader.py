import asyncio
import re
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

from downloader_bot.downloaders.base import Downloader
from downloader_bot.models import DownloadError, Media, Quality, SiteHTTPError, Source

from .client import HEADERS, PinterestClient
from .parser import (
    HLSVariant,
    cdn_url,
    hls_attributes,
    hls_duration,
    pin_id,
    read_hls_variants,
    read_pin,
    validate_page_url,
)


class PinterestDownloader(Downloader):
    def __init__(self, client: PinterestClient):
        self.client = client

    def cache_key(self, url: str) -> str:
        validate_page_url(url)
        parts = urlsplit(url)
        return "short:" + parts.path.rstrip("/") if parts.hostname == "pin.it" else pin_id(url)

    async def inspect(self, url: str) -> Media:
        identity = await self.client.identify(url)
        media = read_pin(await self.client.pin(identity), identity)
        formats = await self._formats(media.qualities)
        if not formats:
            raise DownloadError(
                "رسانهٔ کامل و قابل دریافت برای این پین پیدا نشد.",
                code="pinterest_no_playable_format",
            )
        if len(formats) > 80:
            raise DownloadError("تعداد کیفیت‌های این پین بیش از سقف پشتیبانی است.")
        return replace(media, qualities=tuple(quality for quality, _ in formats))

    async def resolve(self, media: Media, quality: Quality) -> Source:
        if media.site != "pinterest" or quality not in media.qualities:
            raise DownloadError("کیفیت انتخاب‌شده معتبر نیست.")
        identity = await self.client.identify(media.page_url)
        if identity != media.content_id:
            raise DownloadError("شناسهٔ پین تغییر کرده است؛ لینک را دوباره بفرست.")
        fresh = read_pin(await self.client.pin(identity), identity)
        # A selected HLS rendition is rediscovered in fresh master playlists of the same asset.
        asset = quality.key.split("_")[1]
        candidates = tuple(
            item
            for item in fresh.qualities
            if (
                item.key.rsplit("_", 1)[0] == quality.key.rsplit("_", 1)[0]
                or (quality.protocol == item.protocol == "hls" and item.key.split("_")[1] == asset)
            )
        )
        if not candidates and quality.protocol == "progressive":
            signature = re.search(r"[0-9a-f]{32}", quality.endpoint)
            same_asset = tuple(item for item in fresh.qualities if item.key.split("_")[1] == asset)
            # Relay A/B responses may omit an encode. Revalidate the published URL only if
            # fresh Pinterest metadata still identifies the exact same underlying video.
            if signature and any(signature[0] in item.endpoint for item in same_asset):
                candidates = (quality,)
        for candidate, source in await self._formats(candidates, quality.key):
            if candidate.key == quality.key:
                return source
        raise DownloadError("کیفیت انتخاب‌شده دیگر در دسترس نیست؛ لینک را دوباره بفرست.")

    async def _formats(
        self,
        qualities: tuple[Quality, ...],
        selected: str | None = None,
    ) -> list[tuple[Quality, Source]]:
        results = await asyncio.gather(
            *(self._format(quality, selected) for quality in qualities), return_exceptions=True
        )
        formats = {}
        errors = []
        for result in results:
            if isinstance(result, SiteHTTPError) and result.status == 429:
                raise result
            if isinstance(result, DownloadError):
                errors.append(result)
            elif isinstance(result, BaseException):
                raise result
            else:
                for quality, source in result:
                    formats.setdefault(quality.key, (quality, source))
        if not formats and errors:
            raise errors[0]
        asset_order = {}
        for quality in qualities:
            asset_order.setdefault(quality.key.split("_")[1], len(asset_order))
        return sorted(
            formats.values(),
            key=lambda item: (
                asset_order[item[0].key.split("_")[1]],
                item[0].protocol != "progressive",
                not item[0].original,
                -(item[0].width or 0) * (item[0].height or 0),
                item[0].codec != "h264",
            ),
        )

    async def _format(self, quality: Quality, selected: str | None) -> list[tuple[Quality, Source]]:
        try:
            if quality.protocol == "progressive":
                probe = await self.client.probe_media(quality.endpoint, quality.mime_type)
                if probe.valid:
                    if quality.mime_type == "video/mp4":
                        old = (
                            f"{quality.width}×{quality.height}"
                            if quality.width and quality.height
                            else "ابعاد نامشخص"
                        )
                        dimensions = (
                            f"{probe.width}×{probe.height}"
                            if probe.width and probe.height
                            else "ابعاد نامشخص"
                        )
                        quality = replace(
                            quality,
                            width=probe.width,
                            height=probe.height,
                            duration=probe.duration
                            if probe.duration is not None
                            else quality.duration,
                            key=quality.key.rsplit("_", 1)[0]
                            + f"_{probe.width or 0}x{probe.height or 0}",
                            label=quality.label.replace(old, dimensions).rsplit(" · ", 1)[0]
                            + f" · {(probe.codec or 'video').upper()}",
                            codec=probe.codec or "video",
                        )
                    if not selected or selected == quality.key:
                        if probe.size_bytes:
                            size = (
                                f"{probe.size_bytes / 1048576:.1f} MB"
                                if probe.size_bytes >= 1048576
                                else f"{probe.size_bytes / 1024:.0f} KB"
                            )
                            quality = replace(quality, label=quality.label + f" · {size}")
                        return [
                            (
                                quality,
                                Source(
                                    quality.endpoint,
                                    "progressive",
                                    HEADERS.copy(),
                                    size_bytes=probe.size_bytes,
                                ),
                            )
                        ]
                return []
            manifest, url = await self.client.manifest(quality.endpoint)
            self._validate_manifest(manifest, url)
            if "#EXT-X-STREAM-INF:" not in manifest:
                hls_duration(manifest, url, quality.duration or 0)
                return [(replace(quality, endpoint=url), Source(url, "hls", HEADERS.copy()))]
            variants = read_hls_variants(manifest, url)
            variants = tuple(
                item
                for item in variants
                if not selected or self._quality(quality, item).key == selected
            )
            results = await asyncio.gather(
                *(self._variant(quality, item) for item in variants), return_exceptions=True
            )
            available = []
            errors = []
            for result in results:
                if isinstance(result, DownloadError):
                    errors.append(result)
                elif isinstance(result, BaseException):
                    raise result
                elif result:
                    available.append(result)
            if not available and errors:
                raise errors[0]
            return available
        except SiteHTTPError as error:
            if error.status not in {401, 403, 404, 410, 416}:
                raise
        except DownloadError as error:
            if error.__cause__ is not None:
                raise
        return []

    @staticmethod
    def _quality(base: Quality, variant: HLSVariant) -> Quality:
        quality = variant.quality()
        prefix = base.label.split("HLS", 1)[0]
        return replace(
            quality,
            key=f"v_{base.key.split('_')[1]}_{quality.key}",
            label=prefix + quality.label,
            duration=base.duration,
        )

    async def _variant(self, base: Quality, variant: HLSVariant) -> tuple[Quality, Source] | None:
        try:
            manifest, url = await self.client.manifest(variant.url)
            self._validate_manifest(manifest, url)
            duration = hls_duration(manifest, url, base.duration or 0)
            audio_url = None
            if variant.audio_url:
                audio, audio_url = await self.client.manifest(variant.audio_url)
                self._validate_manifest(audio, audio_url)
                audio_duration = hls_duration(audio, audio_url, duration)
                if abs(audio_duration - duration) > max(3, duration * 0.05):
                    raise DownloadError("مدت صوت و تصویر HLS با هم مطابقت ندارد.")
            quality = replace(self._quality(base, variant), endpoint=url, duration=duration)
            return quality, Source(url, "hls", HEADERS.copy(), audio_url)
        except SiteHTTPError as error:
            if error.status not in {401, 403, 404, 410}:
                raise
        except DownloadError as error:
            if error.__cause__ is not None:
                raise
        return None

    @staticmethod
    def _validate_manifest(manifest: str, url: str) -> None:
        for line in manifest.splitlines():
            line = line.strip()
            candidate = hls_attributes(line).get("URI") if line.startswith("#") else line
            if candidate and not cdn_url(urljoin(url, candidate)):
                raise DownloadError("فهرست HLS به آدرسی خارج از CDN Pinterest اشاره می‌کند.")
