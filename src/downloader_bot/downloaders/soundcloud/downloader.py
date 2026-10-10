import asyncio
import time
from dataclasses import replace

import httpx

from downloader_bot.downloaders.base import Downloader
from downloader_bot.schemas.media import DownloadError, Media, Quality, SiteHTTPError, Source

from .client import SoundCloudClient
from .parser import (
    is_preview_url,
    read_qualities,
    read_track,
    unavailable_error,
    validate_page_url,
)
from .urls import cache_alias


class SoundCloudDownloader(Downloader):
    def __init__(
        self, client: SoundCloudClient, accounts: dict[str, SoundCloudClient] | None = None
    ):
        self.client = client
        self.accounts = accounts or {}
        self._next_account = 0
        self._cooldowns: dict[str, float] = {}
        self._resolve_slots = asyncio.Semaphore(4)

    def cache_key(self, url: str) -> str:
        validate_page_url(url)
        return cache_alias(url)

    def catalog_key(self, url: str) -> None:
        # A permalink slug can change or be reassigned to a different track.
        # Always rediscover its numeric track ID after the short metadata TTL.
        validate_page_url(url)
        return None

    async def inspect(self, url: str) -> Media:
        ready = [key for key in self.accounts if self._cooldowns.get(key, 0) <= time.monotonic()]
        if not ready:
            return await self._inspect(url, self.client, "guest")
        account_id = ready[self._next_account % len(ready)]
        self._next_account += 1
        try:
            return await self._inspect(url, self.accounts[account_id], account_id)
        except SiteHTTPError as error:
            if error.status == 429:
                self._cooldowns[account_id] = time.monotonic() + 600
                raise  # Do not conceal a rate limit by immediately retrying with another account.
            if error.status in (401, 403):
                self._cooldowns[account_id] = time.monotonic() + 600
                return await self._inspect(url, self.client, "guest")
            raise

    async def _inspect(self, url: str, client: SoundCloudClient, account_id: str) -> Media:
        page, final_url = await client.page(url)
        await client.public_client_id(page)
        from_page = False
        try:
            track = read_track(page)
        except DownloadError:
            track = await client.api(
                "https://api-v2.soundcloud.com/resolve", params={"url": final_url}
            )
            if track.get("kind") != "track":
                raise DownloadError("فعلاً لینک یک آهنگ را بفرست؛ لینک مجموعه پشتیبانی نشده است.")
        else:
            if client.headers:
                track = await self._fresh_track(client, str(track["id"]))
            else:
                from_page = True
        failure = None
        try:
            available = await self._qualities(client, track)
        except (DownloadError, httpx.HTTPError, TimeoutError) as error:
            if isinstance(error, SiteHTTPError) and error.status == 429:
                raise
            available, failure = [], error
        if not available and from_page:
            # Public HTML can retain transcodings that the current playback API removed.
            track = await self._fresh_track(client, str(track["id"]))
            failure = None
            available = await self._qualities(client, track)
        if not available:
            if failure is not None:
                raise failure
            raise unavailable_error(track)
        duration = int(track.get("duration", 0) / 1000)
        return Media(
            "soundcloud",
            str(track["id"]),
            track["title"],
            track.get("user", {}).get("username", ""),
            duration,
            track.get("permalink_url", final_url),
            track.get("artwork_url"),
            tuple(available),
            track.get("track_authorization"),
            account_id,
        )

    async def _qualities(self, client: SoundCloudClient, track: dict) -> list[Quality]:
        if track.get("policy") in ("BLOCK", "SNIP"):
            raise DownloadError(
                "نسخه کامل این آهنگ با دسترسی فعلی قابل دریافت نیست.",
                code="soundcloud_access_restricted",
            )
        qualities = list(read_qualities(track))
        # Offer only endpoints that resolve successfully; broken ABR and previews are excluded.
        duration = int(track.get("duration", 0) / 1000)
        resolved = await asyncio.gather(
            *(
                self._playback(client, quality, track.get("track_authorization"), duration)
                for quality in qualities
            ),
            return_exceptions=True,
        )
        available, failure = [], None
        for result in resolved:
            if isinstance(result, SiteHTTPError) and result.status == 429:
                raise result
            if isinstance(result, (DownloadError, httpx.HTTPError, TimeoutError)):
                failure = result
                continue
            if isinstance(result, BaseException):
                raise result
            if result is not None:
                available.append(result[0])
        if track.get("downloadable") and track.get("has_downloads_left"):
            try:
                if original := await self._original(client, track):
                    available.insert(0, original)
            except (DownloadError, httpx.HTTPError, TimeoutError) as error:
                if isinstance(error, SiteHTTPError) and error.status == 429:
                    raise
                failure = error
        if not available and failure is not None:
            raise failure
        return available

    @staticmethod
    async def _fresh_track(client: SoundCloudClient, identity: str) -> dict:
        track = await client.api(f"https://api-v2.soundcloud.com/tracks/{identity}")
        if str(track.get("id")) != identity:
            raise DownloadError(
                "شناسه پاسخ SoundCloud با آهنگ درخواستی یکسان نیست.",
                code="soundcloud_identity_mismatch",
            )
        return track

    async def resolve(self, media: Media, quality: Quality) -> Source:
        if media.site != "soundcloud" or quality not in media.qualities:
            raise DownloadError("کیفیت انتخاب‌شده متعلق به این آهنگ نیست.")
        if media.account_id != "guest" and media.account_id not in self.accounts:
            raise DownloadError("حساب این درخواست دیگر فعال نیست؛ لینک را دوباره بفرست.")
        try:
            client = self.client if media.account_id == "guest" else self.accounts[media.account_id]
            if not quality.original:
                playback = await self._playback(
                    client, quality, media.authorization, media.duration
                )
                if playback is None:
                    track = await self._fresh_track(client, media.content_id)
                    for candidate in read_qualities(track):
                        if candidate.key == quality.key:
                            playback = await self._playback(
                                client, candidate, track.get("track_authorization"), media.duration
                            )
                            break
                if playback is None:
                    raise DownloadError(
                        "نسخه کامل این کیفیت فعلاً قابل دریافت نیست؛ لینک آهنگ را دوباره بفرست."
                    )
                return playback[1]
            data = await client.api(quality.endpoint)
        except httpx.HTTPError as error:
            raise DownloadError("ارتباط با SoundCloud قطع شد؛ دوباره امتحان کن.") from error
        url = data.get("redirectUri")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise DownloadError("لینک دریافت این کیفیت در پاسخ SoundCloud وجود ندارد.")
        return Source(url, quality.protocol, duration=media.duration or None)

    async def _original(self, client: SoundCloudClient, track: dict) -> Quality | None:
        endpoint = f"https://api-v2.soundcloud.com/tracks/{track['id']}/download"
        try:
            data = await client.api(endpoint)
        except SiteHTTPError as error:
            if error.status in (401, 403, 404):
                return None
            raise
        url = data.get("redirectUri")
        if not isinstance(url, str) or not url.startswith("https://"):
            return None
        try:
            # Account headers remain on the API; signed CDN links do not need them.
            response = await client.http.head(url, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError:
            return None  # An unavailable Original must not hide the playable alternatives.
        mime = response.headers.get("content-type", "application/octet-stream").split(";")[0]
        extension = {
            "audio/wav": "wav",
            "audio/x-wav": "wav",
            "audio/flac": "flac",
            "audio/mpeg": "mp3",
            "audio/mp4": "m4a",
        }.get(mime, "bin")
        return Quality(
            f"original_{extension}",
            f"Original ({extension.upper()})",
            "original",
            None,
            extension,
            mime,
            "progressive",
            endpoint,
            original=True,
        )

    async def _playback(
        self, client: SoundCloudClient, quality: Quality, authorization: str | None, duration: int
    ) -> tuple[Quality, Source] | None:
        candidates = [quality]
        if quality.fallback_endpoint:
            candidates.append(
                replace(
                    quality,
                    endpoint=quality.fallback_endpoint,
                    protocol="hls",
                    fallback_endpoint=None,
                )
            )
        async with self._resolve_slots:
            failure = None
            for candidate in candidates:
                try:
                    data = await client.api(candidate.endpoint, authorization)
                except SiteHTTPError as error:
                    if error.status in (401, 403, 404):
                        continue
                    raise
                except (httpx.HTTPError, TimeoutError) as error:
                    failure = error
                    continue
                url = data.get("url")
                if (
                    not isinstance(url, str)
                    or not url.startswith("https://")
                    or is_preview_url(url, duration)
                ):
                    continue
                return candidate, Source(url, candidate.protocol, duration=duration or None)
            if failure is not None:
                raise failure
        return None
