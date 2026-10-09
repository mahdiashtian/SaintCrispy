import asyncio
import re
import time
from dataclasses import dataclass, field
from math import ceil
from urllib.parse import urljoin

import httpx

from downloader_bot.models import DownloadError, SiteHTTPError, Source

from .parser import HLSSample, media_url, mp4_dimensions, validate_page_url

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.xvideos.com/",
}


@dataclass(frozen=True)
class ReadResult:
    data: bytes
    url: str = field(repr=False)
    size: int | None
    content_type: str


def mp4_header(data: bytes, *, segment: bool = False) -> bool:
    offset = 0
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        if kind == b"ftyp":
            return size >= 16 and offset + 16 <= len(data)
        if segment and kind in {b"styp", b"moof", b"mdat", b"sidx"}:
            return size == 0 or size >= 8
        if kind not in {b"free", b"skip", b"wide"} or size < 8:
            break
        offset += size
    return False


class XVideosClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self._slots = asyncio.Semaphore(4)
        self._cooldown_until = 0.0

    def _check_cooldown(self) -> None:
        remaining = ceil(self._cooldown_until - time.monotonic())
        if remaining > 0:
            raise SiteHTTPError(
                429, f"XVideos درخواست‌ها را محدود کرده؛ {remaining} ثانیه بعد امتحان کن."
            )

    async def page(self, url: str) -> tuple[str, str]:
        result = await self._read(url, HEADERS, 4 * 1024 * 1024, page=True)
        return result.data.decode("utf-8", errors="replace"), result.url

    @staticmethod
    def source_headers(page_url: str) -> dict[str, str]:
        validate_page_url(page_url)
        return {**HEADERS, "Referer": page_url}

    async def manifest(self, url: str, page_url: str) -> tuple[str, str]:
        result = await self._read(url, self.source_headers(page_url), 256 * 1024)
        return result.data.decode("utf-8-sig", errors="replace"), result.url

    async def mp4_source(self, url: str, page_url: str) -> Source | None:
        result = await self.mp4_probe(url, page_url)
        return result[0] if result else None

    async def mp4_probe(
        self, url: str, page_url: str
    ) -> tuple[Source, tuple[int, int] | None] | None:
        result = await self._read(url, self.source_headers(page_url), 1024, sample=True)
        if not mp4_header(result.data):
            return None
        return (
            Source(
                result.url, "progressive", self.source_headers(page_url), size_bytes=result.size
            ),
            mp4_dimensions(result.data),
        )

    async def is_mp4(self, url: str, page_url: str) -> bool:
        return await self.mp4_source(url, page_url) is not None

    async def sample(self, sample: HLSSample, page_url: str) -> bool:
        result = await self._read(
            sample.url,
            self.source_headers(page_url),
            sample.length,
            sample=True,
            offset=sample.offset,
        )
        if sample.kind == "key":
            return len(result.data) == 16
        if result.content_type.startswith("text/") or "json" in result.content_type:
            return False
        if sample.kind == "encrypted":
            return len(result.data) >= 16
        if mp4_header(result.data, segment=sample.kind == "media"):
            return True
        data = result.data
        if sample.kind == "media":
            return (
                len(data) >= 376
                and data[0] == data[188] == 0x47
                or len(data) >= 4
                and data[0] == 0xFF
                and data[1] & 0xE0 == 0xE0
                or data.startswith(b"ID3")
            )
        return False

    @staticmethod
    def _sample_size(response: httpx.Response, offset: int, limit: int) -> int | None:
        if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            raise DownloadError("پاسخ نمونه رسانه رمزگذاری انتقال نامعتبر دارد.")
        if response.status_code == 206:
            match = re.fullmatch(
                r"bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})",
                response.headers.get("content-range", ""),
            )
            if not match:
                raise DownloadError("پاسخ محدوده رسانه معتبر نیست.")
            start, end, total = map(int, match.groups())
            if start != offset or not start <= end < total or end >= offset + limit:
                raise DownloadError("پاسخ محدوده رسانه معتبر نیست.")
            return total
        if offset:
            raise DownloadError("سرور محدوده درخواستی قطعه HLS را رعایت نکرد.")
        length = response.headers.get("content-length", "")
        return int(length) if re.fullmatch(r"[0-9]{1,20}", length) and int(length) > 0 else None

    async def _read(
        self,
        url: str,
        headers: dict[str, str],
        limit: int,
        *,
        page: bool = False,
        sample: bool = False,
        offset: int = 0,
    ) -> ReadResult:
        retries = redirects = 0
        if sample:
            headers = {
                **headers,
                "Range": f"bytes={offset}-{offset + limit - 1}",
                "Accept-Encoding": "identity",
            }
        try:
            async with asyncio.timeout(20 if page else 10):
                while redirects < 6:
                    self._check_cooldown()
                    if page:
                        validate_page_url(url)
                    elif not media_url(url, url):
                        raise DownloadError("آدرس رسانه معتبر نیست.")
                    try:
                        async with self._slots:
                            # A queued request may have waited while another received HTTP 429.
                            self._check_cooldown()
                            async with self.http.stream(
                                "GET",
                                url,
                                headers=headers,
                                follow_redirects=False,
                                timeout=10,
                            ) as response:
                                if response.is_redirect and "location" in response.headers:
                                    url = urljoin(url, response.headers["location"])
                                    redirects += 1
                                    continue
                                if response.status_code == 429:
                                    value = response.headers.get("retry-after", "")
                                    delay = (
                                        min(900, max(1, int(value)))
                                        if re.fullmatch(r"[0-9]{1,9}", value)
                                        else 60
                                    )
                                    self._cooldown_until = time.monotonic() + delay
                                    self._check_cooldown()
                                retry = (
                                    response.status_code in {408, 500, 502, 503, 504}
                                    and retries == 0
                                )
                                if not retry:
                                    if not response.is_success:
                                        raise SiteHTTPError(
                                            response.status_code,
                                            f"دریافت از XVideos ناموفق بود (HTTP {response.status_code}).",
                                        )
                                    total = (
                                        self._sample_size(response, offset, limit)
                                        if sample
                                        else None
                                    )
                                    data = bytearray()
                                    async for chunk in response.aiter_bytes(
                                        min(limit, 1024) if sample else 64 * 1024
                                    ):
                                        data.extend(chunk)
                                        if sample and len(data) >= limit:
                                            break
                                        if len(data) > limit:
                                            raise DownloadError(
                                                "حجم پاسخ اطلاعات رسانه بیش از حد است."
                                            )
                                    if (
                                        sample
                                        and total is not None
                                        and len(data) < min(limit, total - offset)
                                    ):
                                        raise DownloadError("نمونه رسانه ناقص دریافت شد.")
                                    return ReadResult(
                                        bytes(data[:limit]),
                                        str(response.url),
                                        total,
                                        response.headers.get("content-type", "").lower(),
                                    )
                    except (httpx.TransportError, httpx.TimeoutException):
                        if retries:
                            raise
                    retries += 1
                    await asyncio.sleep(0.2)
        except (httpx.HTTPError, TimeoutError) as error:
            raise DownloadError("ارتباط با XVideos قطع شد؛ دوباره امتحان کن.") from error
        raise DownloadError("تعداد تغییر مسیر لینک بیش از حد است.")
