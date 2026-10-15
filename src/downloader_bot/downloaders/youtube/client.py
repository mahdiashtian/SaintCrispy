import asyncio
import json
import logging
import os
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx

from core.bounded_curl import BoundedSession
from core.source_http import CurlTransport
from schemas.media import DownloadError, Media, Quality, SiteHTTPError, Source

from .parser import (
    canonical_url,
    codec,
    public_download_url,
    public_metadata,
    public_video_info,
    source_headers,
    usable,
)
from .urls import DEFAULT_RESOLVER, PUBLIC_METADATA_URL, PUBLIC_ORIGIN

log = logging.getLogger(__name__)
RETRYABLE_EXTRACTION = {
    "youtube_login_required",
    "youtube_extraction_failed",
    "youtube_cdn_forbidden",
    "youtube_cdn_unavailable",
    "youtube_cdn_connection_failed",
}


@dataclass(frozen=True)
class YouTubeClient:
    """Own isolated native extraction and anonymous public recovery sessions."""

    js_runtime: str = "node"
    cookies_file: str | None = field(default=None, repr=False)
    proxy: str = field(default="", repr=False)
    dns_resolver: str = field(default="", repr=False)
    resolver_url: str = field(default="", repr=False)
    resolver_proxy: str = field(default="", repr=False)
    player_clients: str | None = None
    pot_base_url: str | None = None
    timeout: float = 80
    _cookies_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False, compare=False
    )
    _cooldowns: dict[str, float] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _public_cache: OrderedDict = field(
        default_factory=OrderedDict, init=False, repr=False, compare=False
    )
    _public_cooldown: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    _public_slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(2), init=False, repr=False, compare=False
    )
    _public_locks: tuple = field(
        default_factory=lambda: tuple(asyncio.Lock() for _ in range(32)),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self):
        if self.proxy and urlsplit(self.proxy).scheme != "http":
            raise DownloadError("YOUTUBE_PROXY باید نشانی یک پراکسی HTTP باشد.")
        if self.resolver_url:
            parsed = urlsplit(self.resolver_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "YouTube resolver must be an HTTPS base URL without credentials, query or fragment"
                )

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
            dns_resolver=(
                os.environ.get("YOUTUBE_DOH_URL", "") if not os.environ.get("YOUTUBE_PROXY") else ""
            ),
            resolver_url=os.environ.get("YOUTUBE_RESOLVER_URL", DEFAULT_RESOLVER),
            resolver_proxy=os.environ.get("YOUTUBE_PROXY", ""),
            player_clients=player_clients,
            pot_base_url=pot_base_url,
        )

    async def _public_json(self, http, method, url, **options):
        if self._public_cooldown.get("until", 0) > time.monotonic():
            raise SiteHTTPError(429, "مسیر عمومی یوتیوب موقتاً محدود شده؛ کمی بعد امتحان کن.")
        async with http.stream(method, url, **options) as response:
            operation = (
                "request_key"
                if url.endswith("/key")
                else "convert"
                if url.endswith("/converter")
                else "metadata"
            )
            log.debug(
                {
                    "event": "youtube_public_response",
                    "operation": operation,
                    "http_status": response.status_code,
                }
            )
            if response.status_code == 429:
                self._public_cooldown["until"] = time.monotonic() + 60
                raise SiteHTTPError(429, "مسیر عمومی یوتیوب موقتاً محدود شده؛ کمی بعد امتحان کن.")
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(8192):
                body.extend(chunk)
                if len(body) > 1024 * 1024:
                    raise ValueError("Public metadata exceeds limit")
        return json.loads(body)

    @asynccontextmanager
    async def _public_session(self):
        async with BoundedSession(
            impersonate="chrome",
            discard_cookies=True,
            trust_env=False,
            proxy=self.resolver_proxy or None,
        ) as browser:
            async with httpx.AsyncClient(
                transport=CurlTransport(browser),
                trust_env=False,
                timeout=12,
                headers={"Origin": PUBLIC_ORIGIN, "Referer": PUBLIC_ORIGIN + "/"},
            ) as api:
                # Preserve the real impersonation profile instead of HTTPX's default UA.
                api.headers.pop("User-Agent", None)
                yield api

    async def _public_convert(self, api, identity, kind, value, seconds):
        async with self._public_slots:
            link = canonical_url(identity)
            key = await self._public_json(
                api, "GET", self.resolver_url.rstrip("/") + "/v2/sanity/key", params={"link": link}
            )
            key = key.get("key") if isinstance(key, dict) else None
            if not isinstance(key, str) or not 1 <= len(key) <= 8192 or "\n" in key or "\r" in key:
                raise ValueError("Missing public request key")
            result = await self._public_json(
                api,
                "POST",
                self.resolver_url.rstrip("/") + "/v2/converter",
                json={
                    "link": link,
                    "format": kind,
                    "audioBitrate": value if kind == "mp3" else "128",
                    "videoQuality": value if kind == "mp4" else "360",
                    "vCodec": "h264",
                    "filenameStyle": "pretty",
                },
                headers={"key": key},
            )
            if not isinstance(result, dict) or result.get("status") not in {"tunnel", "redirect"}:
                raise ValueError("Public conversion unavailable")
            target = public_download_url(result.get("url"), identity)
            if not target:
                raise ValueError("Public result identity or destination mismatch")
            source = Source(
                target,
                "progressive",
                headers={"Referer": PUBLIC_ORIGIN + "/"},
                proxy=self.resolver_proxy,
                duration=seconds,
                require_audio=kind == "mp4",
            )
            async with httpx.AsyncClient(
                trust_env=False, proxy=self.resolver_proxy or None, timeout=12
            ) as cdn:
                async with cdn.stream(
                    "GET",
                    target,
                    headers={
                        **source.headers,
                        "Range": "bytes=0-262143",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    response.raise_for_status()
                    sample = bytearray()
                    async for chunk in response.aiter_bytes(16384):
                        sample.extend(chunk)
                        if len(sample) >= 256 * 1024:
                            break
            if kind == "mp4":
                width, height, audio, family = public_video_info(sample)
                if not width or not height or not audio or family != "h264":
                    raise ValueError("Missing playable public MP4 header")
                quality = Quality(
                    f"public_video_{height}p_h264_mp4",
                    f"{height}p · H264 · MP4",
                    "h264",
                    None,
                    "mp4",
                    "video/mp4",
                    "progressive",
                    f"public:mp4:{value}",
                    width=width,
                    height=height,
                    duration=seconds,
                )
            else:
                if not (
                    sample.startswith(b"ID3")
                    or any(
                        sample[index] == 255 and sample[index + 1] & 224 == 224
                        for index in range(min(1024, len(sample) - 1))
                    )
                ):
                    raise ValueError("Missing public MP3 header")
                quality = Quality(
                    f"public_audio_mp3_{value}",
                    f"MP3 · {value}kbps",
                    "mp3",
                    int(value),
                    "mp3",
                    "audio/mpeg",
                    "progressive",
                    f"public:mp3:{value}",
                    duration=seconds,
                )
            return quality, source

    async def public_inspect(self, identity):
        if not self.resolver_url or self.cookies_file:
            raise DownloadError(code="youtube_public_disabled")
        try:
            async with asyncio.timeout(35):
                async with self._public_locks[hash(identity) % len(self._public_locks)]:
                    return await self._public_inspect(identity)
        except TimeoutError as error:
            raise DownloadError(
                "دریافت اطلاعات مسیر عمومی یوتیوب طولانی شد؛ کمی بعد امتحان کن.",
                code="youtube_public_unavailable",
            ) from error

    async def _public_inspect(self, identity):
        cached = self._public_cache.get(identity)
        if cached and cached[0] > time.monotonic():
            self._public_cache.move_to_end(identity)
            return cached[1]
        try:
            async with asyncio.timeout(35):
                async with self._public_session() as api:
                    metadata = await self._public_json(
                        api, "GET", PUBLIC_METADATA_URL, params={"q": identity}
                    )
                    title, author, seconds = public_metadata(metadata, identity)
                    results = await asyncio.gather(
                        *(
                            self._public_convert(api, identity, kind, value, seconds)
                            for kind, value in (
                                ("mp4", "360"),
                                ("mp4", "720"),
                                ("mp4", "1080"),
                                ("mp3", "128"),
                            )
                        ),
                        return_exceptions=True,
                    )
                rows = {}
                for result in results:
                    if isinstance(result, SiteHTTPError) and result.status == 429:
                        raise result
                    if isinstance(result, (httpx.HTTPError, TimeoutError, ValueError)):
                        log.warning(
                            {
                                "event": "youtube_public_quality_unavailable",
                                "error_type": type(result).__name__,
                            }
                        )
                        continue
                    if isinstance(result, BaseException):
                        raise result
                    quality, source = result
                    rows.setdefault(quality.key, (quality, source))
                if not rows:
                    raise ValueError("No verified public sources")
                media = Media(
                    "youtube",
                    identity,
                    title,
                    author,
                    seconds,
                    canonical_url(identity),
                    f"https://i.ytimg.com/vi/{identity}/hqdefault.jpg",
                    tuple(q for q, _ in rows.values()),
                    account_id="public_resolver",
                )
                self._public_cache[identity] = (time.monotonic() + 15, media, rows)
                self._public_cache.move_to_end(identity)
                while len(self._public_cache) > 128:
                    self._public_cache.popitem(last=False)
                log.info({"event": "youtube_public_extracted", "qualities": len(rows)})
                return media
        except SiteHTTPError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            log.warning(
                {"event": "youtube_public_inspection_failed", "error_type": type(error).__name__}
            )
            raise DownloadError(
                "مسیر عمومی دریافت این ویدیو پاسخ قابل استفاده نداد.",
                code="youtube_public_unavailable",
            ) from error

    async def public_resolve(self, media, quality):
        if not self.resolver_url or self.cookies_file:
            raise DownloadError(code="youtube_public_disabled")
        try:
            async with asyncio.timeout(30):
                async with self._public_locks[hash(media.content_id) % len(self._public_locks)]:
                    return await self._public_resolve(media, quality)
        except TimeoutError as error:
            raise DownloadError(
                "دریافت لینک تازه یوتیوب طولانی شد؛ کمی بعد امتحان کن.",
                code="youtube_public_unavailable",
            ) from error

    async def _public_resolve(self, media, quality):
        cached = self._public_cache.get(media.content_id)
        if cached and cached[0] > time.monotonic():
            candidate = cached[2].get(quality.key)
            if candidate and candidate[0] == quality:
                expiry = parse_qs(urlsplit(candidate[1].url).query).get("exp", [""])[0]
                if not expiry.isdigit() or int(expiry) / 1000 > time.time() + 10:
                    return candidate[1]
        parts = quality.endpoint.split(":")
        if (
            len(parts) != 3
            or parts[0] != "public"
            or (parts[1], parts[2])
            not in {("mp4", "360"), ("mp4", "720"), ("mp4", "1080"), ("mp3", "128")}
        ):
            raise DownloadError("کیفیت انتخاب‌شده معتبر نیست.")
        try:
            async with asyncio.timeout(30):
                async with self._public_session() as api:
                    current, source = await self._public_convert(
                        api, media.content_id, parts[1], parts[2], media.duration
                    )
            if (current.key, current.width, current.height) != (
                quality.key,
                quality.width,
                quality.height,
            ):
                raise ValueError("Public rendition changed")
            rows = dict(cached[2]) if cached and cached[1] == media else {}
            rows[quality.key] = (quality, source)
            self._public_cache[media.content_id] = (time.monotonic() + 15, media, rows)
            self._public_cache.move_to_end(media.content_id)
            while len(self._public_cache) > 128:
                self._public_cache.popitem(last=False)
            return source
        except SiteHTTPError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            raise DownloadError(
                "دریافت لینک تازه این کیفیت ناموفق بود؛ لینک پست را دوباره بفرست.",
                code="youtube_public_unavailable",
            ) from error

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
        return await self._run_extraction(url)

    async def extract_verified(self, url: str, verify):
        """Try the alternate player when metadata exists but no complete source works."""
        return await self._run_extraction(url, verify)

    async def _run_extraction(self, url: str, verify=None):
        if self.cookies_file:
            # yt-dlp can update its cookie file on exit; serialize workers for this account.
            async with self._cookies_lock:
                return await self._extract_strategies(url, verify)
        return await self._extract_strategies(url, verify)

    async def _extract_strategies(self, url: str, verify=None):
        for code, until in self._cooldowns.items():
            if time.monotonic() < until:
                raise extraction_error(b"429" if code == "youtube_rate_limited" else b"not a bot")
        # Retry once with a different supported player, inside one overall deadline.
        # An explicit override remains authoritative; private/429 failures never loop.
        strategies = [self.player_clients]
        if not self.player_clients and not self.cookies_file:
            strategies.append("web_embedded")
        try:
            async with asyncio.timeout(self.timeout):
                for index, clients in enumerate(strategies):
                    try:
                        data = await self._extract(url, clients)
                        choices = await verify(data) if verify else None
                        if verify and not choices:
                            raise DownloadError(
                                "لینک کیفیت‌های یوتیوب روی اتصال سرور قابل دریافت نیست.",
                                code="youtube_cdn_unavailable",
                            )
                        self._cooldowns.clear()
                        log.info({"event": "youtube_extracted", "attempt": index + 1})
                        return (data, choices) if verify else data
                    except DownloadError as error:
                        if isinstance(error, SiteHTTPError) and error.status == 429:
                            self._cooldowns["youtube_rate_limited"] = time.monotonic() + 60
                            raise
                        log.warning(
                            {
                                "event": "youtube_extraction_attempt_failed",
                                "attempt": index + 1,
                                "error_code": error.code,
                            }
                        )
                        if error.code not in RETRYABLE_EXTRACTION or index == len(strategies) - 1:
                            if error.code in {"youtube_login_required", "youtube_rate_limited"}:
                                self._cooldowns[error.code] = time.monotonic() + 60
                            raise
        except TimeoutError as error:
            raise DownloadError(
                "دریافت اطلاعات یوتیوب طولانی شد. کمی بعد دوباره امتحان کن.",
                code="youtube_extraction_timeout",
            ) from error

    async def _extract(self, url: str, player_clients: str | None = None) -> dict:
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
            "" if self.dns_resolver else self.proxy,
        ]
        if self.dns_resolver:
            arguments[1:3] = [
                "-c",
                "from downloader_bot.downloaders.youtube.client import extractor_main; extractor_main()",
            ]
        if self.cookies_file:
            arguments.extend(["--cookies", self.cookies_file])
        if player_clients:
            arguments.extend(["--extractor-args", f"youtube:player_client={player_clients}"])
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
                **(
                    {"env": dict(os.environ, SAINTCRISPY_YOUTUBE_DOH_URL=self.dns_resolver)}
                    if self.dns_resolver
                    else {}
                ),
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


def extractor_main():
    """CLI worker: isolate synchronous upstream DNS from Telegram's event loop."""
    import yt_dlp

    from core.dns_tunnel import extractor_dns

    from .urls import network_host

    with extractor_dns(os.environ["SAINTCRISPY_YOUTUBE_DOH_URL"], network_host):
        yt_dlp.main()


def extraction_error(diagnostic: bytes) -> DownloadError:
    # Do not expose stderr: it may contain credentials or temporary signed URLs.
    text = diagnostic.lower()
    if b"no module named yt_dlp" in text:
        return DownloadError("وابستگی yt-dlp روی سرور نصب نشده است.", code="youtube_missing_yt_dlp")
    # HTTP throttling must win over incidental sign-in/challenge warnings.
    if b"429" in text and (b"http error 429" in text or b"too many requests" in text):
        return DownloadError(
            "یوتیوب درخواست‌های سرور را موقتاً محدود کرده؛ کمی بعد امتحان کن.",
            code="youtube_rate_limited",
        )
    # Classify the fatal error rather than a warning from an earlier player.
    fatal = [line for line in text.splitlines() if line.startswith(b"error:")]
    if fatal:
        text = fatal[-1]
    if any(
        marker in text
        for marker in (
            b"timed out",
            b"timeout",
            b"network is unreachable",
            b"no route to host",
            b"connection refused",
            b"connection reset",
            b"unable to resolve",
            b"name or service not known",
            b"getaddrinfo failed",
            b"temporary failure in name resolution",
        )
    ):
        return DownloadError(
            "ارتباط سرور با یوتیوب برقرار نشد؛ مدیر ربات باید مسیر شبکه را بررسی کند.",
            code="youtube_connection_failed",
        )
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
            "یوتیوب از اتصال ربات تأیید ورود می‌خواد و فایل رو نمی‌ده. "
            "برای ادامه، مدیر ربات باید نشست یوتیوب یا اتصال سرور رو تنظیم کنه.",
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
