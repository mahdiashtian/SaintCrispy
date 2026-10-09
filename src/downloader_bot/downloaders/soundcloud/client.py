import asyncio
import json
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx

from downloader_bot.models import DownloadError, SiteHTTPError

from .parser import read_hydration, validate_page_url


class SoundCloudClient:
    """Async access to public web metadata and optional account playback."""

    def __init__(self, http: httpx.AsyncClient, oauth_token: str | None = None):
        self.http = http
        self.headers = {"Authorization": f"OAuth {oauth_token}"} if oauth_token else {}
        self.client_id: str | None = None
        self._client_id_lock = asyncio.Lock()
        self._cooldown_until = 0.0

    def _check_cooldown(self) -> None:
        if time.monotonic() < self._cooldown_until:
            raise SiteHTTPError(
                429, "SoundCloud درخواست‌ها را محدود کرده؛ کمی بعد دوباره امتحان کن."
            )

    async def page(self, url: str) -> tuple[str, str]:
        # Validate every redirect before requesting it, including short links.
        for _ in range(6):
            self._check_cooldown()
            validate_page_url(url)
            response = await self.http.get(url, follow_redirects=False)
            self._check_status(response, allow_redirect=True)
            if response.is_redirect and "location" in response.headers:
                url = urljoin(url, response.headers["location"])
                continue
            return response.text, str(response.url)
        raise DownloadError("تعداد تغییر مسیر لینک بیش از حد است.")

    async def public_client_id(
        self, page: str | None = None, *, invalid_id: str | None = None
    ) -> str:
        async with self._client_id_lock:
            if self.client_id and self.client_id != invalid_id:
                return self.client_id
            if page is None:
                page, _ = await self.page("https://soundcloud.com/")
            for item in read_hydration(page):
                if item.get("hydratable") != "apiClient":
                    continue
                data = item.get("data")
                value = data.get("id") if isinstance(data, dict) else None
                if (
                    isinstance(value, str)
                    and value != invalid_id
                    and re.fullmatch(r"[0-9a-zA-Z]{32}", value)
                ):
                    self.client_id = value
                    return value
            scripts = re.findall(r'<script[^>]+src="([^"]+)"', page)
            for url in reversed(scripts[-8:]):
                if urlsplit(url).hostname != "a-v2.sndcdn.com":
                    continue
                response = await self.http.get(url)
                if response.status_code == 404:
                    continue
                self._check_status(response)
                match = re.search(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"', response.text)
                if match and match[1] != invalid_id:
                    self.client_id = match[1]
                    return self.client_id
        raise DownloadError(
            "شناسه عمومی پخش SoundCloud پیدا نشد؛ ساختار سایت نیاز به بررسی دارد.",
            code="soundcloud_client_id_unavailable",
        )

    async def api(
        self, url: str, authorization: str | None = None, *, params: dict[str, str] | None = None
    ) -> dict:
        self._check_cooldown()
        if urlsplit(url).hostname != "api-v2.soundcloud.com" or not url.startswith("https://"):
            raise DownloadError("آدرس API رسانه معتبر نیست.")
        query = {**(params or {}), "client_id": self.client_id or await self.public_client_id()}
        if authorization:
            query["track_authorization"] = authorization
        response = await self.http.get(url, params=query, headers=self.headers)
        # Original download uses 401/403 for account permissions, not a stale public ID.
        if response.status_code in (401, 403) and not urlsplit(url).path.endswith("/download"):
            try:
                refreshed = await self.public_client_id(invalid_id=query["client_id"])
            except DownloadError as error:
                if error.code != "soundcloud_client_id_unavailable":
                    raise
                refreshed = query["client_id"]  # Preserve the original permission response.
            if refreshed != query["client_id"]:
                query["client_id"] = refreshed
                response = await self.http.get(url, params=query, headers=self.headers)
        self._check_status(response)
        try:
            data = response.json()
        except json.JSONDecodeError as error:
            raise DownloadError("پاسخ SoundCloud معتبر نیست.") from error
        if not isinstance(data, dict):
            raise DownloadError("پاسخ SoundCloud معتبر نیست.", code="soundcloud_invalid_response")
        return data

    def _check_status(self, response: httpx.Response, allow_redirect: bool = False) -> None:
        if response.status_code == 429:
            self._cooldown_until = time.monotonic() + 60
            raise SiteHTTPError(
                429, "SoundCloud درخواست‌ها را محدود کرده؛ کمی بعد دوباره امتحان کن."
            )
        if response.status_code in (401, 403):
            raise SiteHTTPError(
                response.status_code,
                "برای این کیفیت دسترسی حساب لازم است یا دسترسی فعلی کافی نیست.",
            )
        if response.is_success or (allow_redirect and response.is_redirect):
            return
        raise SiteHTTPError(
            response.status_code,
            f"دریافت اطلاعات SoundCloud ناموفق بود (HTTP {response.status_code}).",
        )
