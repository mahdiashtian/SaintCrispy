import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from downloader_bot.schemas.media import DownloadError, SiteHTTPError

from .parser import codec, source_headers, usable


@dataclass(frozen=True)
class YouTubeClient:
    """Run yt-dlp outside the bot's event loop; only metadata crosses stdout."""

    js_runtime: str = "node"
    cookies_file: str | None = field(default=None, repr=False)
    proxy: str = field(default="", repr=False)
    player_clients: str | None = None
    pot_base_url: str | None = None
    timeout: float = 80
    _cookies_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False, compare=False
    )

    def __post_init__(self):
        if self.proxy and urlsplit(self.proxy).scheme != "http":
            raise DownloadError("YOUTUBE_PROXY باید نشانی یک پراکسی HTTP باشد.")

    @classmethod
    def from_environment(cls):
        pot_base_url = os.environ.get("YOUTUBE_POT_BASE_URL") or None
        player_clients = os.environ.get("YOUTUBE_PLAYER_CLIENTS") or None
        if pot_base_url and not player_clients:
            # yt-dlp recommends mweb for GVS tokens; a configured helper must be used.
            player_clients = "mweb,web_safari"
        return cls(
            js_runtime=os.environ.get("YOUTUBE_JS_RUNTIME") or "node",
            cookies_file=os.environ.get("YOUTUBE_COOKIES_FILE") or None,
            proxy=os.environ.get("YOUTUBE_PROXY", ""),
            player_clients=player_clients,
            pot_base_url=pot_base_url,
        )

    async def available(self, items: list[dict]) -> set[str]:
        """Small same-egress samples; never list a format solely from metadata."""
        slots = asyncio.Semaphore(4)
        async with httpx.AsyncClient(proxy=self.proxy or None, trust_env=False, timeout=10) as http:

            async def check(item):
                hls = item.get("protocol") in {"m3u8", "m3u8_native"}
                headers = source_headers(item)
                if not hls:
                    headers["Range"] = "bytes=0-4095"
                async with (
                    slots,
                    http.stream(
                        "GET",
                        item["url"],
                        headers=headers,
                        follow_redirects=True,
                    ) as response,
                ):
                    if response.status_code in {401, 403, 404, 410, 416}:
                        return None
                    if response.status_code == 429:
                        raise SiteHTTPError(
                            429, "یوتیوب درخواست‌های سرور را موقتاً محدود کرده؛ کمی بعد امتحان کن."
                        )
                    response.raise_for_status()
                    sample = bytearray()
                    async for chunk in response.aiter_bytes(4096):
                        sample.extend(chunk)
                        if (not hls and len(sample) >= 4096) or len(sample) > 256 * 1024:
                            break
                    valid = (
                        sample.startswith(b"#EXTM3U") and b"#EXT-X-ENDLIST" in sample
                        if hls
                        else b"ftyp" in sample[:32] or sample.startswith(b"\x1aE\xdf\xa3")
                    )
                    return item["url"] if valid else None

            results = await asyncio.gather(*(check(item) for item in items), return_exceptions=True)
            available, failure = set(), None
            for result in results:
                if isinstance(result, SiteHTTPError) and result.status == 429:
                    raise result
                if isinstance(result, (httpx.HTTPError, TimeoutError)):
                    failure = result
                    continue
                if isinstance(result, BaseException):
                    raise result
                if result:
                    available.add(result)
            if not available and failure is not None:
                raise DownloadError(
                    "ارتباط با جریان‌های یوتیوب قطع شد؛ دوباره امتحان کن.",
                    code="youtube_cdn_connection_failed",
                ) from failure
            return available

    async def extract(self, url: str) -> dict:
        if self.cookies_file:
            # yt-dlp can update its cookie file on exit; serialize workers for this account.
            async with self._cookies_lock:
                return await self._extract(url)
        return await self._extract(url)

    async def _extract(self, url: str) -> dict:
        arguments = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-cache-dir",
            "--no-playlist",
            "--skip-download",
            "--dump-single-json",
            "--no-progress",
            "--no-remote-components",
            "--js-runtimes",
            self.js_runtime,
            "--socket-timeout",
            "15",
            "--retries",
            "1",
            "--extractor-retries",
            "1",
            "--proxy",
            self.proxy,
        ]
        if self.cookies_file:
            arguments.extend(["--cookies", self.cookies_file])
        if self.player_clients:
            arguments.extend(["--extractor-args", f"youtube:player_client={self.player_clients}"])
        if self.pot_base_url:
            arguments.extend(
                [
                    "--extractor-args",
                    f"youtubepot-bgutilhttp:base_url={self.pot_base_url}",
                ]
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                "--",
                url,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise DownloadError("پردازش استخراج یوتیوب روی سرور اجرا نشد.") from error

        async def read_bounded(stream, limit: int) -> bytes:
            result = bytearray()
            while chunk := await stream.read(65536):
                result.extend(chunk)
                if len(result) > limit:
                    raise DownloadError("پاسخ استخراج یوتیوب بیش از حد بزرگ است.")
            return bytes(result)

        stdout = asyncio.create_task(read_bounded(process.stdout, 16 * 1024 * 1024))
        stderr = asyncio.create_task(read_bounded(process.stderr, 256 * 1024))
        try:
            async with asyncio.timeout(self.timeout):
                output, diagnostic = await asyncio.gather(stdout, stderr)
                await process.wait()
            if process.returncode:
                raise extraction_error(diagnostic)
            try:
                data = json.loads(output)
            except (ValueError, UnicodeError) as error:
                raise DownloadError("پاسخ استخراج یوتیوب معتبر نبود.") from error
            if not isinstance(data, dict):
                raise DownloadError("پاسخ استخراج یوتیوب معتبر نبود.")
            if not any(usable(item) and codec(item) for item in (data.get("formats") or [])):
                raise extraction_error(diagnostic)
            return data
        except TimeoutError as error:
            raise DownloadError("دریافت اطلاعات یوتیوب طولانی شد؛ دوباره امتحان کن.") from error
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            for task in (stdout, stderr):
                task.cancel()
            await asyncio.gather(stdout, stderr, return_exceptions=True)


def extraction_error(diagnostic: bytes) -> DownloadError:
    # Do not expose stderr: it may contain credentials or temporary signed URLs.
    text = diagnostic.lower()
    if b"no module named yt_dlp" in text:
        return DownloadError("وابستگی yt-dlp روی سرور نصب نشده است.", code="youtube_missing_yt_dlp")
    if b"private video" in text or b"members-only" in text:
        return DownloadError(
            "این ویدیو به حساب دارای دسترسی نیاز دارد.", code="youtube_private_video"
        )
    if any(
        marker in text for marker in (b"age-restricted", b"age restricted", b"confirm your age")
    ):
        return DownloadError(
            "این ویدیو به حساب مجاز برای محدودیت سنی نیاز دارد.", code="youtube_age_restricted"
        )
    if b"not a bot" in text or b"sign in" in text:
        return DownloadError(
            "یوتیوب فعلاً دریافت این ویدیو را روی اتصال ربات محدود کرده؛ کمی بعد دوباره امتحان کن.",
            code="youtube_login_required",
        )
    if b"429" in text:
        return DownloadError(
            "یوتیوب درخواست‌های سرور را موقتاً محدود کرده؛ کمی بعد امتحان کن.",
            code="youtube_rate_limited",
        )
    if b"403" in text:
        return DownloadError(
            "یوتیوب اجازه دریافت این جریان را نداد؛ لینک را دوباره بفرست.",
            code="youtube_cdn_forbidden",
        )
    if any(
        marker in text
        for marker in (b"javascript", b"js runtime", b"signature solving failed", b"n challenge")
    ):
        return DownloadError(
            "حل چالش JavaScript یوتیوب روی سرور تنظیم نشده یا ناموفق بوده است.",
            code="youtube_js_challenge_failed",
        )
    return DownloadError(
        "دریافت اطلاعات یوتیوب ممکن نشد؛ ویدیو یا اتصال سرور را بررسی کن.",
        code="youtube_extraction_failed",
    )
