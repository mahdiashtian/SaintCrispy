import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

import httpx
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from schemas.media import DownloadError, SiteHTTPError

from .parser import cdn_url, content_path, find_media, media_id, page_media, validate_page_url

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "X-IG-App-ID": "936619743392459",
    "X-IG-WWW-Claim": "0",
}
ROOT_QUERY = "PolarisLoggedOutDesktopWWWPostRootContentQuery"
log = logging.getLogger(__name__)


class InstagramResponseStream(httpx.AsyncByteStream):
    def __init__(self, response):
        self.response = response

    async def __aiter__(self):
        try:
            async for chunk in self.response.aiter_content():
                yield chunk
        except RequestException as error:
            raise httpx.ReadError("Instagram response interrupted") from error

    async def aclose(self):
        await self.response.aclose()


class InstagramBrowserTransport(httpx.AsyncBaseTransport):
    """Browser TLS/HTTP2 fingerprint; httpx alone owns the account cookie jar."""

    def __init__(self, session):
        self.session = session

    async def handle_async_request(self, request):
        try:
            response = await self.session.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                data=(await request.aread()) or None,
                allow_redirects=False,
                stream=True,
                timeout=10,
            )
        except RequestException as error:
            raise httpx.ConnectError("Instagram connection failed", request=request) from error
        return httpx.Response(
            response.status_code,
            # libcurl has already decoded Content-Encoding on the response stream.
            headers=[
                (key, value)
                for key, value in response.headers.multi_items()
                if key.lower() not in {"content-encoding", "content-length"}
            ],
            stream=InstagramResponseStream(response),
        )


def validate_proxy(proxy: str) -> None:
    if proxy:
        try:
            parts = urlsplit(proxy)
            valid = parts.scheme == "http" and bool(parts.hostname) and parts.port != 0
        except ValueError:
            valid = False
        if not valid or "\r" in proxy or "\n" in proxy:
            raise DownloadError("INSTAGRAM_PROXY باید نشانی یک پراکسی HTTP باشد.")


@asynccontextmanager
async def instagram_session(proxy: str = ""):
    # Never reuse this session for another provider or another Instagram account.
    validate_proxy(proxy)
    async with AsyncSession(
        impersonate="chrome", discard_cookies=True, trust_env=False, proxy=proxy or None
    ) as browser:
        async with httpx.AsyncClient(
            transport=InstagramBrowserTransport(browser), timeout=15, trust_env=False
        ) as http:
            yield http


def lsd_token(page: str) -> str:
    match = re.search(r'\["LSD",\[\],\{"token":"([^"\r\n]+)"', page)
    if match:
        return match[1]
    eqmc = re.search(r'<script[^>]*\bid="__eqmc"[^>]*>(.*?)</script>', page, re.S)
    if eqmc:
        try:
            value = json.loads(eqmc[1]).get("l")
            return value if isinstance(value, str) else ""
        except (ValueError, AttributeError):
            pass
    return ""


def dtsg_token(page: str) -> str:
    match = re.search(r'\["DTSG(?:Init|Initial)Data",\[\],\{"token":"([^"\r\n]+)"', page)
    return match[1] if match else ""


class InstagramClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        cookie: str | None = None,
        *,
        proxy: str = "",
        root_doc_id: str = "27130156389949648",
        shortcode_doc_id: str = "27128499623469141",
        web_doc_id: str = "27830990013244856",
    ):
        self.http = http
        self.cookie = cookie
        validate_proxy(proxy)
        self.proxy = proxy
        if cookie and ("\r" in cookie or "\n" in cookie):
            raise ValueError("INSTAGRAM_COOKIE must be one HTTP Cookie header line")
        if cookie:
            jar = SimpleCookie()
            jar.load(cookie)
            if not jar:
                raise ValueError("INSTAGRAM_COOKIE must contain valid cookies")
            for name, value in jar.items():
                self.http.cookies.set(name, value.value, domain=".instagram.com", path="/")
        self.root_doc_id = root_doc_id
        self.shortcode_doc_id = shortcode_doc_id
        self.web_doc_id = web_doc_id
        self._slots = asyncio.Semaphore(4)
        self._cooldown_until = 0.0
        self._session_lock = asyncio.Lock()
        self._session_token = ""
        self._session_dtsg = ""
        self._session_until = 0.0

    async def _bootstrap(self, failures=None) -> str:
        async with self._session_lock:
            if time.monotonic() < self._session_until:
                return self._session_token
            page = await self._optional_page("https://www.instagram.com/", failures=failures)
            self._session_token = lsd_token(page)
            self._session_dtsg = dtsg_token(page)
            self._session_until = time.monotonic() + (300 if self._session_token else 10)
            return self._session_token

    @staticmethod
    def source_headers(page_url: str) -> dict[str, str]:
        validate_page_url(page_url)
        return {"User-Agent": HEADERS["User-Agent"], "Referer": page_url}

    async def content(self, url: str) -> tuple[dict, str, str]:
        try:
            return await self._content(url)
        except TimeoutError as error:
            raise DownloadError(
                "دریافت اطلاعات اینستاگرام روی اتصال ربات طولانی شد؛ کمی بعد امتحان کن.",
                code="instagram_extraction_timeout",
            ) from error

    async def _content(self, url: str) -> tuple[dict, str, str]:
        kind, code = content_path(url)
        if time.monotonic() < self._cooldown_until:
            raise SiteHTTPError(
                429, "اینستاگرام محدودیت موقت اعمال کرده؛ کمی بعد دوباره امتحان کن."
            )
        async with asyncio.timeout(60):
            # Per-inspection evidence; concurrent posts must not share diagnostics.
            failures = []
            if kind == "share":
                _, final_url = await self._read(url, headers={"User-Agent": "curl/7.88.1"})
                kind, code = content_path(final_url)
                if kind == "share":
                    raise DownloadError("لینک اشتراک resolve نشد؛ لینک اصلی پست یا ریلز را بفرست.")
            if kind == "story":
                return await self._story(code)
            page_url = f"https://www.instagram.com/p/{code}/"
            if self.cookie:
                response = await self._optional_json(
                    f"https://www.instagram.com/api/v1/media/{media_id(code)}/info/",
                    failures=failures,
                )
                if node := find_media(response, code):
                    return node, page_url, code
            # Authorized REST access does not need a homepage LSD bootstrap.
            # A rejected homepage must not consume that account's request budget.
            token = (
                await self._bootstrap(failures)
                if isinstance(self.http._transport, InstagramBrowserTransport)
                else ""
            )
            page = await self._optional_page(page_url, failures=failures)
            if node := page_media(page, code):
                log.info({"event": "instagram_extracted", "method": "page", "content_id": code})
                return node, page_url, code
            token = token or lsd_token(page)
            await self._optional_json(
                "https://www.instagram.com/api/v1/web/get_ruling_for_content/",
                params={"content_type": "MEDIA", "target_id": media_id(code)},
                failures=failures,
            )
            response = await self._graphql(
                "https://www.instagram.com/api/graphql",
                self.root_doc_id,
                ROOT_QUERY,
                {"media_id": media_id(code)},
                page_url,
                token,
                failures,
            )
            if node := find_media(response, code):
                log.info({"event": "instagram_extracted", "method": "graphql", "content_id": code})
                return node, page_url, code
            # Current Instagrapi web query has a different document and variables
            # from the legacy Instaloader shortcode endpoint. Keep both bounded.
            response = await self._graphql(
                "https://www.instagram.com/api/graphql",
                self.web_doc_id,
                "PolarisPostRootQuery",
                {
                    "shortcode": code,
                    "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
                    "__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": True,
                },
                page_url,
                token,
                failures,
                dtsg=dtsg_token(page) or self._session_dtsg,
            )
            if node := find_media(response, code):
                log.info(
                    {"event": "instagram_extracted", "method": "web_graphql", "content_id": code}
                )
                return node, page_url, code
            response = await self._graphql(
                "https://www.instagram.com/graphql/query",
                self.shortcode_doc_id,
                None,
                {
                    "shortcode": code,
                    "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
                },
                page_url,
                token,
                failures,
            )
            if node := find_media(response, code):
                return node, page_url, code
            embed = await self._optional_page(page_url + "embed/captioned/", failures=failures)
            if node := page_media(embed, code):
                return node, page_url, code
            if failures:
                failure = next(
                    (error for error in failures if error.code == "instagram_login_required"),
                    failures[-1],
                )
                log.warning({"event": "instagram_extraction_failed", "error_code": failure.code})
                raise failure
            raise DownloadError(
                "نتونستم فایل این پست رو از اینستاگرام بگیرم. لینک رو کمی بعد دوباره بفرست.",
                code="instagram_empty_media",
            )

    async def _optional_page(self, url: str, *, failures=None) -> str:
        # A missing/blocked web page does not prove the public media APIs are empty.
        try:
            data, _ = await self._read(url)
            return data.decode("utf-8", errors="replace")
        except SiteHTTPError as error:
            if error.status == 401 and failures is not None:
                failures.append(login_error())
            if error.status not in {401, 403, 404, 410, 500, 502, 503, 504}:
                raise
        except DownloadError as error:
            if not isinstance(error.__cause__, (httpx.HTTPError, TimeoutError)):
                raise
            if failures is not None:
                failures.append(error)
        return ""

    async def _story(self, code: str) -> tuple[dict, str, str]:
        if not self.cookie:
            raise DownloadError("دریافت استوری به نشست مجاز اینستاگرام در تنظیمات ربات نیاز دارد.")
        username, story_id = code.split("/")
        response = await self._optional_json(
            "https://www.instagram.com/api/v1/users/web_profile_info/",
            params={"username": username},
        )
        user = (response.get("data") or {}).get("user") or {}
        if not user.get("id"):
            raise DownloadError("حساب استوری در دسترس نشست اینستاگرام نیست.")
        response = await self._optional_json(
            "https://i.instagram.com/api/v1/feed/reels_media/", params={"reel_ids": user["id"]}
        )
        for reel in (response.get("reels") or {}).values():
            for item in reel.get("items") or []:
                if str(item.get("pk")) == story_id:
                    return (
                        {**item, "user": item.get("user") or user},
                        (f"https://www.instagram.com/stories/{username}/{story_id}/"),
                        "story_" + story_id,
                    )
        raise DownloadError("استوری منقضی شده یا برای نشست اینستاگرام قابل مشاهده نیست.")

    async def _graphql(
        self, url, doc_id, name, variables, page_url, token, failures=None, *, dtsg=""
    ) -> dict:
        csrf = next((c.value for c in self.http.cookies.jar if c.name == "csrftoken"), "")
        headers = {
            "X-CSRFToken": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": page_url,
            "Origin": "https://www.instagram.com",
            "Accept": "*/*",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        data = {
            "server_timestamps": "true",
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": doc_id,
        }
        if name:
            headers["X-FB-Friendly-Name"] = name
            data.update(
                fb_api_caller_class="RelayModern",
                fb_api_req_friendly_name=name,
            )
            if token:
                headers["X-FB-LSD"] = token
                data["lsd"] = token
        if dtsg:
            data["fb_dtsg"] = dtsg
        # The shortcode document uses Instaloader's POST envelope; the root
        # document uses Relay/LSD. They are independent requests, not aliases.
        return await self._optional_json(
            url,
            failures=failures,
            method="POST",
            headers=headers,
            data=data,
        )

    async def _optional_json(self, url: str, *, failures=None, **kwargs) -> dict:
        try:
            data, _ = await self._read(url, **kwargs)
        except SiteHTTPError as error:
            if error.status == 401 and failures is not None:
                failures.append(login_error())
                return {}
            if error.status not in {403, 404, 405, 410, 500, 502, 503, 504}:
                raise
            return {}
        except DownloadError as error:
            if not isinstance(error.__cause__, (httpx.HTTPError, TimeoutError)):
                raise
            if failures is not None:
                failures.append(error)
            return {}
        try:
            result = json.loads(data.decode("utf-8").removeprefix("for (;;);"))
            if not isinstance(result, dict):
                return {}
            if result.get("errors"):
                errors = result["errors"]
                if isinstance(errors, list) and any(
                    isinstance(error, dict)
                    and (
                        str(error.get("code")) == "1675004"
                        or any(
                            marker in str(error.get("message", "")).lower()
                            for marker in ("rate limit", "please wait", "too many requests")
                        )
                    )
                    for error in errors
                ):
                    self._cooldown_until = time.monotonic() + 60
                    raise SiteHTTPError(
                        429, "اینستاگرام محدودیت موقت اعمال کرده؛ کمی بعد امتحان کن."
                    )
                if failures is not None:
                    failures.append(
                        DownloadError(
                            "اینستاگرام درخواست اطلاعات این پست را روی اتصال ربات رد کرد. "
                            "مدیر ربات باید نشست یا اتصال اینستاگرام را بررسی کند.",
                            code="instagram_graphql_unavailable",
                        )
                    )
                log.warning(
                    {"event": "instagram_query_failed", "error_code": "instagram_graphql_error"}
                )
            elif (
                isinstance(result.get("data"), dict)
                and result["data"].get("xig_polaris_media", True) is None
            ):
                log.info({"event": "instagram_query_empty", "error_code": "instagram_null_media"})
            message = str(result.get("message", "")).lower()
            if result.get("status") == "fail" and any(
                word in message for word in ("please wait", "rate limit", "too many requests")
            ):
                self._cooldown_until = time.monotonic() + 60
                raise SiteHTTPError(429, "اینستاگرام محدودیت موقت اعمال کرده؛ کمی بعد امتحان کن.")
            if result.get("status") == "fail" and any(
                word in message
                for word in (
                    "login_required",
                    "challenge_required",
                    "checkpoint_required",
                )
            ):
                if failures is not None:
                    failures.append(login_error())
                    return {}
                raise login_error()
            return result
        except (ValueError, RecursionError):
            return {}

    async def sample(
        self, url: str, page_url: str, *, metadata: bool = False, tail: bool = False
    ) -> bytes:
        limit = 1024 * 1024 if metadata else 1024
        data, _ = await self._read(
            url,
            headers={
                **self.source_headers(page_url),
                "Range": f"bytes=-{limit}" if tail else f"bytes=0-{limit - 1}",
                "Accept-Encoding": "identity",
            },
            cdn=True,
            sample_limit=limit,
        )
        return data

    async def _read(
        self,
        url: str,
        *,
        method: str = "GET",
        headers=None,
        params=None,
        data=None,
        cdn: bool = False,
        sample_limit: int = 0,
    ) -> tuple[bytes, str]:
        request_headers = {**({} if cdn else HEADERS), **(headers or {})}
        try:
            async with self._slots, asyncio.timeout(15):
                for _ in range(6):
                    if cdn:
                        if not cdn_url(url):
                            raise DownloadError("آدرس CDN اینستاگرام معتبر نیست.")
                    else:
                        validate_page_url(url)
                        if urlsplit(url).path.startswith(("/accounts/login", "/challenge/")):
                            raise SiteHTTPError(
                                401, "نشست مجاز اینستاگرام نیازمند ورود یا تأیید دوباره است."
                            )
                    async with self.http.stream(
                        method,
                        url,
                        headers=request_headers,
                        params=params,
                        data=data,
                        follow_redirects=False,
                        timeout=10,
                    ) as response:
                        if response.is_redirect and "location" in response.headers:
                            url = urljoin(str(response.url), response.headers["location"])
                            params = None
                            if response.status_code in (301, 302, 303):
                                method, data = "GET", None
                            continue
                        if not response.is_success:
                            status = response.status_code
                            if status in (401, 429) and not cdn:
                                if status == 429:
                                    self._cooldown_until = time.monotonic() + 60
                                raise SiteHTTPError(
                                    status,
                                    "اینستاگرام محدودیت موقت یا الزام ورود اعمال کرده؛ کمی بعد امتحان کن.",
                                )
                            raise SiteHTTPError(
                                status, f"دریافت از اینستاگرام ناموفق بود (HTTP {status})."
                            )
                        buffer = bytearray()
                        async for chunk in response.aiter_bytes(16384):
                            buffer.extend(chunk)
                            if sample_limit and len(buffer) >= sample_limit:
                                return bytes(buffer[:sample_limit]), str(response.url)
                            if len(buffer) > 4 * 1024 * 1024:
                                raise DownloadError("حجم پاسخ اطلاعات اینستاگرام بیش از حد است.")
                        return bytes(buffer), str(response.url)
        except (httpx.HTTPError, TimeoutError) as error:
            raise DownloadError(
                "ارتباط با اینستاگرام قطع شد؛ دوباره امتحان کن.",
                code="instagram_connection_failed",
            ) from error
        raise DownloadError("تعداد تغییر مسیر لینک اینستاگرام بیش از حد است.")


def login_error() -> DownloadError:
    return DownloadError(
        "اینستاگرام برای این اتصال ورود یا تأیید نشست می‌خواهد. "
        "مدیر ربات باید نشست مجاز اینستاگرام را تنظیم یا تازه کند.",
        code="instagram_login_required",
    )
