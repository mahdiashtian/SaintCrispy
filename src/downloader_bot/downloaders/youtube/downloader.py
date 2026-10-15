import asyncio
import time
from dataclasses import replace

from downloader_bot.downloaders.base import Downloader
from schemas.media import DownloadError, Media, Quality, SiteHTTPError, Source

from .client import YouTubeClient
from .parser import canonical_url, number, read_media, selections, video_id

RECOVERABLE = {
    "youtube_login_required",
    "youtube_cdn_forbidden",
    "youtube_cdn_unavailable",
    "youtube_cdn_connection_failed",
    "youtube_extraction_failed",
    "youtube_age_restricted",
    "youtube_private_video",
    "youtube_extraction_timeout",
}
ACCOUNT_ATTEMPT_SECONDS = 25


class YouTubeDownloader(Downloader):
    def __init__(
        self,
        client: YouTubeClient,
        accounts: dict[str, YouTubeClient] | None = None,
    ):
        self.client = client
        self.accounts = accounts or {}
        self._next_account = 0
        self._public_preferred_until = 0.0

    def cache_key(self, url: str) -> str:
        return video_id(url)

    async def inspect(self, url: str) -> Media:
        try:
            async with asyncio.timeout(85):
                preflight_seconds = 0.0
                public = bool(getattr(self.client, "resolver_url", "")) and not getattr(
                    self.client, "cookies_file", None
                )
                public_failure = None
                prefer_public = time.monotonic() < self._public_preferred_until
                if (
                    public
                    and not self.accounts
                    and not prefer_public
                    and not getattr(self.client, "player_clients", None)
                    and not getattr(self.client, "pot_base_url", None)
                ):
                    started = time.monotonic()
                    metadata = await self.client.origin_metadata(video_id(url))
                    preflight_seconds = time.monotonic() - started
                    # An exact guest-playable VOD can take the verified public
                    # route immediately instead of waiting for a JS worker first.
                    prefer_public = bool(metadata and metadata.playable)
                if public and not self.accounts and prefer_public:
                    try:
                        media = await self.client.public_inspect(video_id(url))
                        self._public_preferred_until = time.monotonic() + 300
                        return media
                    except SiteHTTPError:
                        raise
                    except DownloadError as error:
                        public_failure = error
                        self._public_preferred_until = 0.0
                try:
                    budget = 85
                    if public:
                        budget = 45 if self.accounts else getattr(self.client, "native_timeout", 8)
                        if not self.accounts:
                            budget = max(0.05, budget - preflight_seconds)
                    async with asyncio.timeout(budget):
                        return await self._inspect(url)
                except DownloadError as error:
                    if not public or error.code not in {
                        "youtube_login_required",
                        "youtube_extraction_failed",
                        "youtube_cdn_forbidden",
                        "youtube_cdn_unavailable",
                        "youtube_cdn_connection_failed",
                        "youtube_connection_failed",
                        "youtube_extraction_timeout",
                        "youtube_js_challenge_failed",
                    }:
                        raise
                except TimeoutError:
                    if not public:
                        raise
                if public_failure is not None:
                    raise public_failure
                media = await self.client.public_inspect(video_id(url))
                if not self.accounts:
                    self._public_preferred_until = time.monotonic() + 300
                return media
        except TimeoutError as error:
            raise DownloadError(
                "دریافت اطلاعات یوتیوب طولانی شد. کمی بعد دوباره امتحان کن.",
                code="youtube_extraction_timeout",
            ) from error

    async def _inspect(self, url: str) -> Media:
        identity = video_id(url)
        clients = []
        if self.accounts:
            rows = list(self.accounts.items())
            offset = self._next_account % len(rows)
            self._next_account += 1
            clients.extend(rows[offset:] + rows[:offset])
        clients = clients[:3]
        if not any(client is self.client for _, client in clients):
            # Keep a default fallback even when the database has many accounts.
            default = next(
                ((name, client) for name, client in self.accounts.items() if client is self.client),
                ("guest", self.client),
            )
            clients = clients[:2]
            clients.append(default)
        # Bound account failover; a bad first cookie must not hide a healthy account.
        failure = None
        for account_id, client in clients:
            try:
                # Reserve time for another account when the first worker stalls.
                budget = ACCOUNT_ATTEMPT_SECONDS if len(clients) > 1 else 80
                async with asyncio.timeout(budget):
                    data, choices = await self._extract_available(client, identity)
                media = read_media(data, identity)
                if not choices:
                    raise DownloadError(
                        "لینک کیفیت‌های یوتیوب روی اتصال سرور قابل دریافت نیست.",
                        code="youtube_cdn_unavailable",
                    )
                return replace(
                    media, qualities=tuple(item.quality for item in choices), account_id=account_id
                )
            except DownloadError as error:
                if error.code not in RECOVERABLE:
                    raise
                failure = error
            except TimeoutError:
                failure = DownloadError(
                    "دریافت اطلاعات یوتیوب طولانی شد. کمی بعد دوباره امتحان کن.",
                    code="youtube_extraction_timeout",
                )
        raise failure

    async def _extract_available(self, client, identity, selected=None):
        async def verify(data):
            read_media(data, identity)
            return await self._available(client, data, selected)

        if isinstance(client, YouTubeClient):
            return await client.extract_verified(canonical_url(identity), verify)
        data = await client.extract(canonical_url(identity))
        return data, await verify(data)

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
        if media.account_id == "public_resolver":
            return await self.client.public_resolve(media, quality)
        try:
            client = self.client if media.account_id == "guest" else self.accounts[media.account_id]
        except KeyError:
            raise DownloadError("حساب این درخواست دیگر فعال نیست؛ لینک را دوباره بفرست.") from None
        data, choices = await self._extract_available(client, identity, quality.key)
        for item in choices:
            if item.quality.key == quality.key:
                if (item.quality.width, item.quality.height) != (quality.width, quality.height):
                    raise DownloadError("ابعاد کیفیت انتخاب‌شده تغییر کرده؛ لینک را دوباره بفرست.")
                return item.source(client.proxy, number(data.get("duration")))
        raise DownloadError("کیفیت انتخاب‌شده دیگر موجود نیست؛ لینک را دوباره بفرست.")
