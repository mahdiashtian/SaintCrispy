import asyncio
import json
import re
import time
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

import httpx

from downloader_bot.schemas.media import DownloadError, SiteHTTPError

from .parser import (
    MediaProbe,
    cdn_url,
    mp4_metadata,
    pin_id,
    read_api_pin,
    read_page_pin,
    validate_page_url,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class PinterestClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self._slots = asyncio.Semaphore(4)
        self._cooldown_until = 0.0

    def _check_cooldown(self) -> None:
        if time.monotonic() < self._cooldown_until:
            raise SiteHTTPError(429, "Pinterest محدودیت موقت اعمال کرده؛ کمی بعد دوباره امتحان کن.")

    async def identify(self, url: str) -> str:
        validate_page_url(url)
        if urlsplit(url).hostname != "pin.it":
            return pin_id(url)
        code = re.fullmatch(r"/([a-zA-Z0-9_-]{1,100})/?", urlsplit(url).path)
        if not code:
            raise DownloadError("لینک کوتاه Pinterest معتبر نیست.")
        # Pinterest's own shortener exposes the destination without fetching a full pin page.
        short = f"https://api.pinterest.com/url_shortener/{code[1]}/redirect/"
        try:
            async with self._slots, asyncio.timeout(20):
                self._check_cooldown()
                async with self.http.stream(
                    "GET", short, headers=HEADERS, follow_redirects=False
                ) as response:
                    location = response.headers.get("location")
                    if response.is_redirect and location:
                        return pin_id(urljoin(short, location))
                    self._check_status(response)
        except (httpx.HTTPError, TimeoutError) as error:
            raise DownloadError("ارتباط با Pinterest قطع شد؛ دوباره امتحان کن.") from error
        raise DownloadError("مقصد لینک کوتاه Pinterest دریافت نشد.")

    async def pin(self, identity: str) -> dict:
        if not re.fullmatch(r"\d{1,30}", identity):
            raise DownloadError("شناسهٔ پین معتبر نیست.")
        page = f"https://www.pinterest.com/pin/{identity}/"
        headers = {
            **HEADERS,
            "Accept": "application/json",
            "Referer": page,
            "X-Requested-With": "XMLHttpRequest",
            "X-Pinterest-PWS-Handler": "www/[username].js",
        }
        params = {
            "source_url": f"/pin/{identity}/",
            "data": json.dumps(
                {
                    "options": {"id": identity, "field_set_key": "unauth_react_main_pin"},
                    "context": {},
                }
            ),
        }
        api_pin = None
        try:
            body, _, _ = await self._read(
                "https://www.pinterest.com/resource/PinResource/get/",
                headers,
                2 * 1024 * 1024,
                kind="api",
                params=params,
            )
            api_pin = read_api_pin(body, identity)
        except SiteHTTPError as error:
            if error.status == 429:
                raise
        except DownloadError:
            pass  # The public pin page is an independent metadata source.
        try:
            body, final_url, _ = await self._read(page, HEADERS, 4 * 1024 * 1024, kind="page")
            if pin_id(final_url) != identity:
                raise DownloadError("صفحهٔ پین تغییر کرده است؛ لینک را دوباره بفرست.")
            page_pin = read_page_pin(body.decode("utf-8", errors="replace"), identity)
            if api_pin:
                # Preserve the API duration/metadata while adding published modern encodes.
                self._enrich(api_pin, page_pin)
                return api_pin
            return page_pin
        except SiteHTTPError as error:
            if error.status == 429:
                raise
            if api_pin is not None:
                return api_pin
            raise
        except DownloadError:
            if api_pin is not None:
                return api_pin
            raise

    @staticmethod
    def _enrich(api_pin: dict, page_pin: dict) -> None:
        videos = {}
        stack = [page_pin]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if item.get("id") and item.get("video_urls"):
                    videos[str(item["id"])] = item["video_urls"]
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        stack = [api_pin]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "video_list" in item and str(item.get("id")) in videos:
                    item["video_urls"] = videos[str(item["id"])]
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)

    async def manifest(self, url: str) -> tuple[str, str]:
        body, final_url, _ = await self._read(url, HEADERS, 256 * 1024)
        return body.decode("utf-8-sig", errors="replace"), final_url

    async def probe_media(self, url: str, mime: str) -> MediaProbe:
        limit = 64 * 1024 if mime == "video/mp4" else 1024
        body, _, headers = await self._read(
            url,
            {**HEADERS, "Range": f"bytes=0-{limit - 1}"},
            limit,
            sample=True,
        )
        total = headers.get("content-range", "").rpartition("/")[2]
        if not total.isdigit():
            total = headers.get("content-length", "")
        size = int(total) if total.isdigit() else None
        if mime == "video/mp4":
            probe = mp4_metadata(body)
            if probe.valid and not probe.width and total.isdigit() and int(total) > limit:
                limit = min(int(total), 256 * 1024)
                tail, _, _ = await self._read(
                    url,
                    {**HEADERS, "Range": f"bytes={int(total) - limit}-"},
                    limit,
                    sample=True,
                )
                metadata = mp4_metadata(tail, tail=True)
                if metadata.width:
                    return replace(metadata, size_bytes=size)
            return replace(probe, size_bytes=size)
        signatures = {
            "image/jpeg": body.startswith(b"\xff\xd8\xff"),
            "image/png": body.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/gif": body.startswith((b"GIF87a", b"GIF89a")),
            "image/webp": body.startswith(b"RIFF") and body[8:12] == b"WEBP",
        }
        return MediaProbe(signatures.get(mime, False), size_bytes=size)

    def _check_status(self, response: httpx.Response) -> None:
        if not response.is_success:
            if response.status_code == 429:
                self._cooldown_until = time.monotonic() + 60
            message = (
                "Pinterest محدودیت موقت اعمال کرده؛ کمی بعد دوباره امتحان کن."
                if response.status_code == 429
                else f"دریافت از Pinterest ناموفق بود (HTTP {response.status_code})."
            )
            raise SiteHTTPError(response.status_code, message)

    async def _read(
        self,
        url: str,
        headers: dict,
        limit: int,
        *,
        kind: str = "cdn",
        params: dict | None = None,
        sample: bool = False,
    ) -> tuple[bytes, str, httpx.Headers]:
        try:
            async with self._slots, asyncio.timeout(20):
                self._check_cooldown()
                for _ in range(6):
                    if kind == "cdn":
                        if not cdn_url(url):
                            raise DownloadError("آدرس رسانهٔ Pinterest معتبر نیست.")
                    elif kind == "page":
                        pin_id(url)
                    elif url != "https://www.pinterest.com/resource/PinResource/get/":
                        raise DownloadError("آدرس API Pinterest معتبر نیست.")
                    async with self.http.stream(
                        "GET",
                        url,
                        params=params,
                        headers=headers,
                        follow_redirects=False,
                        timeout=10,
                    ) as response:
                        params = None
                        if response.is_redirect and "location" in response.headers:
                            url = urljoin(str(response.url), response.headers["location"])
                            continue
                        self._check_status(response)
                        body = bytearray()
                        async for chunk in response.aiter_bytes(1024 if sample else 64 * 1024):
                            body.extend(chunk)
                            if sample and len(body) >= limit:
                                return bytes(body[:limit]), str(response.url), response.headers
                            if len(body) > limit:
                                raise DownloadError("حجم پاسخ اطلاعات Pinterest بیش از حد است.")
                        return bytes(body), str(response.url), response.headers
        except (httpx.HTTPError, TimeoutError) as error:
            raise DownloadError("ارتباط با Pinterest قطع شد؛ دوباره امتحان کن.") from error
        raise DownloadError("تعداد تغییر مسیر لینک Pinterest بیش از حد است.")
