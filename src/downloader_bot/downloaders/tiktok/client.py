"""Bounded anonymous TikTok metadata sessions; no browser process on the hot path."""

import asyncio
import json
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urljoin

import httpx

from core.config import ConfigurationError
from core.diagnostics import error_fields
from schemas.media import DownloadError, SiteHTTPError

from .parser import read_native, read_resolver
from .urls import DEFAULT_RESOLVER, content_id, normalize_url, resolver_url

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
log = logging.getLogger(__name__)


@asynccontextmanager
async def tiktok_session(proxy=""):
    async with httpx.AsyncClient(
        proxy=proxy or None,
        trust_env=False,
        timeout=8,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    ) as http:
        yield http


class TikTokClient:
    def __init__(self, http, native_http=None, *, endpoint=DEFAULT_RESOLVER, proxy="", timeout=6):
        try:
            self.endpoint = resolver_url(endpoint)
        except (ValueError, DownloadError):
            raise ConfigurationError(
                "Invalid TikTok resolver", code="invalid_url", fields=("TIKTOK_RESOLVER_URL",)
            ) from None
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 1 <= timeout <= 20
        ):
            raise ConfigurationError(
                "Invalid TikTok timeout",
                code="out_of_range",
                fields=("TIKTOK_METADATA_TIMEOUT_SECONDS",),
            )
        self.http, self.native_http = http, native_http or http
        self.proxy, self.timeout = proxy, timeout
        self._slots = asyncio.Semaphore(4)
        self._resolver_retry_at = 0.0
        self._native_retry_at = 0.0

    @classmethod
    def from_environment(cls, http, native_http=None):
        try:
            timeout = float(os.environ.get("TIKTOK_METADATA_TIMEOUT_SECONDS") or 6)
        except ValueError:
            raise ConfigurationError(
                "Invalid TikTok timeout",
                code="invalid_number",
                fields=("TIKTOK_METADATA_TIMEOUT_SECONDS",),
            ) from None
        return cls(
            http,
            native_http,
            endpoint=os.environ.get("TIKTOK_RESOLVER_URL", DEFAULT_RESOLVER),
            proxy=os.environ.get("TIKTOK_PROXY", ""),
            timeout=timeout,
        )

    async def _read(self, http, method, url, *, limit, anonymous=False, **options):
        request = http.build_request(method, url, headers=HEADERS, **options)
        if anonymous:
            # Resolver requests contain only a public post URL, never account material.
            for name in ("Cookie", "Authorization", "Proxy-Authorization"):
                request.headers.pop(name, None)
        response = await http.send(request, stream=True, follow_redirects=False, auth=None)
        try:
            if response.status_code == 429:
                delay = 30
                try:
                    delay = max(1, min(300, int(response.headers.get("Retry-After", "30"))))
                except ValueError:
                    pass
                if anonymous:
                    self._resolver_retry_at = time.monotonic() + delay
                raise SiteHTTPError(429, "تیک‌تاک محدودیت موقت اعمال کرده؛ کمی بعد امتحان کن.")
            if response.is_redirect:
                return b"", response.headers.get("location")
            if response.status_code != 200:
                raise SiteHTTPError(response.status_code, "پست تیک‌تاک اکنون قابل دریافت نیست.")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > limit:
                    raise DownloadError(
                        "پاسخ تیک‌تاک بیش از سقف مجاز است.", code="tiktok_response_limit"
                    )
            return bytes(raw), None
        finally:
            await response.aclose()

    async def _resolver(self, url):
        raw, redirect = await self._read(
            self.http,
            "POST",
            self.endpoint,
            limit=2 * 1024 * 1024,
            anonymous=True,
            data={"url": url, "hd": "1"},
        )
        if redirect:
            raise DownloadError("پاسخ سرویس تیک‌تاک معتبر نیست.", code="tiktok_resolver_redirect")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise DownloadError(
                "پاسخ سرویس تیک‌تاک معتبر نیست.", code="tiktok_invalid_response"
            ) from None
        return read_resolver(payload, url)

    async def content(self, url):
        url = normalize_url(url)
        resolver_error = None
        if self.endpoint and time.monotonic() >= self._resolver_retry_at:
            try:
                async with asyncio.timeout(self.timeout), self._slots:
                    return await self._resolver(url)
            except (DownloadError, httpx.HTTPError, TimeoutError) as error:
                if isinstance(error, DownloadError) and error.code == "tiktok_identity_mismatch":
                    raise
                resolver_error = error
                log.warning({"event": "tiktok_resolver_failed", **error_fields(error)})
        if time.monotonic() < self._native_retry_at:
            raise DownloadError(
                "ارتباط با تیک‌تاک برقرار نشد؛ کمی بعد امتحان کن.",
                code="tiktok_temporarily_unavailable",
            ) from None
        try:
            async with asyncio.timeout(self.timeout), self._slots:
                current = url
                expected = content_id(url)
                canonical_retry = (
                    expected is None
                    and isinstance(resolver_error, DownloadError)
                    and resolver_error.code == "tiktok_resolver_rejected"
                )
                for _ in range(5):
                    raw, location = await self._read(
                        self.native_http, "GET", current, limit=5 * 1024 * 1024
                    )
                    if location:
                        current = normalize_url(urljoin(current, location))
                        redirected_id = content_id(current)
                        if expected and redirected_id and expected != redirected_id:
                            raise DownloadError(
                                "شناسهٔ پست تغییر کرده است.", code="tiktok_identity_mismatch"
                            )
                        if redirected_id and canonical_retry:
                            # Some resolvers reject a share token but accept its verified ID.
                            # Reuse this attempt's slot/deadline; never restart the budget.
                            canonical_retry = False
                            try:
                                return await self._resolver(current)
                            except (DownloadError, httpx.HTTPError) as error:
                                if (
                                    isinstance(error, DownloadError)
                                    and error.code == "tiktok_identity_mismatch"
                                ):
                                    raise
                                log.warning(
                                    {
                                        "event": "tiktok_canonical_resolver_failed",
                                        **error_fields(error),
                                    }
                                )
                        continue
                    result = read_native(raw.decode("utf-8", errors="replace"), current)
                    # A short redirect must never silently replace a known post identity.
                    if expected and expected != result.media.content_id:
                        raise DownloadError(
                            "شناسهٔ پست تغییر کرده است.", code="tiktok_identity_mismatch"
                        )
                    return result
                raise DownloadError(
                    "مقصد لینک کوتاه تیک‌تاک مشخص نشد.", code="tiktok_short_link_unresolved"
                )
        except (httpx.HTTPError, TimeoutError) as error:
            self._native_retry_at = time.monotonic() + 30
            log.warning({"event": "tiktok_native_failed", **error_fields(error)})
            raise DownloadError(
                "دریافت اطلاعات تیک‌تاک کامل نشد؛ کمی بعد امتحان کن.",
                code="tiktok_extraction_unavailable",
            ) from None
        except DownloadError:
            if isinstance(resolver_error, SiteHTTPError) and resolver_error.status == 429:
                raise resolver_error from None
            raise
