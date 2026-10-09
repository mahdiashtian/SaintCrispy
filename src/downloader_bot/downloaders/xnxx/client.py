import asyncio
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from downloader_bot.schemas.media import DownloadError, SiteHTTPError

from .parser import media_url, validate_page_url
from .urls import with_page_slug

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.xnxx.com/",
}


@dataclass(frozen=True)
class MP4Probe:
    url: str
    size_bytes: int | None


@dataclass(frozen=True)
class _Response:
    data: bytes
    url: str
    status: int
    headers: httpx.Headers


class XNXXClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
    ):
        self.http = http
        self.site = "XNXX"
        self.headers = HEADERS.copy()
        self.validate_page_url = validate_page_url
        self._slots = asyncio.Semaphore(4)
        self._cooldown_until = 0.0

    async def page(self, url: str) -> tuple[str, str]:
        self.validate_page_url(url)
        response = await self._read(
            with_page_slug(url),
            self.headers,
            4 * 1024 * 1024,
            page=True,
        )
        return response.data.decode("utf-8", errors="replace"), response.url

    def source_headers(self, page_url: str) -> dict[str, str]:
        self.validate_page_url(page_url)
        return {**self.headers, "Referer": page_url}

    async def manifest(self, url: str, page_url: str) -> tuple[str, str]:
        response = await self._read(url, self.source_headers(page_url), 256 * 1024)
        return response.data.decode("utf-8-sig", errors="replace"), response.url

    async def is_mp4(self, url: str, page_url: str) -> bool:
        return await self.mp4(url, page_url) is not None

    async def mp4(self, url: str, page_url: str) -> MP4Probe | None:
        headers = {
            **self.source_headers(page_url),
            "Range": "bytes=0-1023",
            "Accept-Encoding": "identity",
        }
        response = await self._read(url, headers, 1024, sample=True)
        data = response.data
        size_bytes = None
        if response.status == 206:
            match = re.fullmatch(
                r"bytes 0-(\d+)/(\d+)",
                response.headers.get("content-range", ""),
            )
            if not match:
                return None
            stop, total = map(int, match.groups())
            if stop >= min(1024, total) or len(data) != stop + 1:
                return None
            size_bytes = total
        elif response.status == 200:
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) >= len(data):
                size_bytes = int(length) or None
        else:
            return None
        # A compressed response does not expose the media's byte size reliably.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            size_bytes = None
        # A status/MIME alone could describe an HTML error page. Inspect the MP4 file type box.
        offset = 0
        while offset + 8 <= len(data):
            size = int.from_bytes(data[offset : offset + 4], "big")
            kind = data[offset + 4 : offset + 8]
            if kind == b"ftyp":
                if size >= 16 and offset + 16 <= len(data):
                    return MP4Probe(response.url, size_bytes)
                break
            if kind not in {b"free", b"skip", b"wide"} or size < 8:
                break
            offset += size
        return None

    async def _read(
        self,
        url: str,
        headers: dict[str, str],
        limit: int,
        *,
        page: bool = False,
        sample: bool = False,
    ) -> _Response:
        try:
            async with self._slots, asyncio.timeout(20 if page else 10):
                for _ in range(6):
                    if time.monotonic() < self._cooldown_until:
                        raise SiteHTTPError(
                            429, "XNXX درخواست‌ها را محدود کرده؛ کمی بعد دوباره امتحان کن."
                        )
                    if page:
                        self.validate_page_url(url)
                    elif not media_url(url, url):
                        raise DownloadError("آدرس رسانه معتبر نیست.")
                    async with self.http.stream(
                        "GET", url, headers=headers, follow_redirects=False, timeout=10
                    ) as response:
                        if response.is_redirect and "location" in response.headers:
                            url = urljoin(url, response.headers["location"])
                            continue
                        if not response.is_success:
                            if response.status_code == 429:
                                self._cooldown_until = time.monotonic() + 60
                            message = (
                                f"{self.site} درخواست‌ها را محدود کرده؛ کمی بعد دوباره امتحان کن."
                                if response.status_code == 429
                                else f"دریافت از {self.site} ناموفق بود (HTTP {response.status_code})."
                            )
                            raise SiteHTTPError(
                                response.status_code,
                                message,
                            )
                        data = bytearray()
                        async for chunk in response.aiter_bytes(1024 if sample else 64 * 1024):
                            data.extend(chunk)
                            if sample and len(data) >= limit:
                                return _Response(
                                    bytes(data[:limit]),
                                    str(response.url),
                                    response.status_code,
                                    response.headers,
                                )
                            if len(data) > limit:
                                raise DownloadError("حجم پاسخ اطلاعات رسانه بیش از حد است.")
                        return _Response(
                            bytes(data),
                            str(response.url),
                            response.status_code,
                            response.headers,
                        )
        except (httpx.HTTPError, TimeoutError) as error:
            raise DownloadError(f"ارتباط با {self.site} قطع شد؛ دوباره امتحان کن.") from error
        raise DownloadError("تعداد تغییر مسیر لینک بیش از حد است.")
