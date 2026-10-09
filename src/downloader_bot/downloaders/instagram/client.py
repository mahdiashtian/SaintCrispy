import asyncio
import json
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx

from downloader_bot.schemas.media import DownloadError, SiteHTTPError

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
SHORTCODE_QUERY = "PolarisPostActionLoadPostQueryQuery"


class InstagramClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        cookie: str | None = None,
        *,
        root_doc_id: str = "27130156389949648",
        shortcode_doc_id: str = "27128499623469141",
    ):
        self.http = http
        self.cookie = cookie
        if cookie and ("\r" in cookie or "\n" in cookie):
            raise ValueError("INSTAGRAM_COOKIE must be one HTTP Cookie header line")
        self.root_doc_id = root_doc_id
        self.shortcode_doc_id = shortcode_doc_id
        self._slots = asyncio.Semaphore(4)
        self._cooldown_until = 0.0

    @staticmethod
    def source_headers(page_url: str) -> dict[str, str]:
        validate_page_url(page_url)
        return {"User-Agent": HEADERS["User-Agent"], "Referer": page_url}

    async def content(self, url: str) -> tuple[dict, str, str]:
        kind, code = content_path(url)
        if time.monotonic() < self._cooldown_until:
            raise SiteHTTPError(
                429, "اینستاگرام محدودیت موقت اعمال کرده؛ کمی بعد دوباره امتحان کن."
            )
        async with asyncio.timeout(60):
            if kind == "share":
                _, final_url = await self._read(url, headers={"User-Agent": "curl/7.88.1"})
                kind, code = content_path(final_url)
                if kind == "share":
                    raise DownloadError("لینک اشتراک resolve نشد؛ لینک اصلی پست یا ریلز را بفرست.")
            if kind == "story":
                return await self._story(code)
            page_url = f"https://www.instagram.com/p/{code}/"
            page, _ = await self._read(page_url)
            page = page.decode("utf-8", errors="replace")
            if node := page_media(page, code):
                return node, page_url, code
            lsd = re.search(r'\["LSD",\[\],\{"token":"([^"\r\n]+)"', page)
            token = lsd[1] if lsd else ""
            await self._optional_json(
                "https://www.instagram.com/api/v1/web/get_ruling_for_content/",
                params={"content_type": "MEDIA", "target_id": media_id(code)},
            )
            response = await self._graphql(
                "https://www.instagram.com/api/graphql",
                self.root_doc_id,
                ROOT_QUERY,
                {"media_id": media_id(code)},
                page_url,
                token,
            )
            if node := find_media(response, code):
                return node, page_url, code
            data = response.get("data")
            if isinstance(data, dict) and data.get("xig_polaris_media", False) is None:
                raise DownloadError("این پست اینستاگرام حذف شده یا برای این نشست قابل دسترسی نیست.")
            response = await self._graphql(
                "https://www.instagram.com/graphql/query",
                self.shortcode_doc_id,
                SHORTCODE_QUERY,
                {
                    "shortcode": code,
                    "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
                },
                page_url,
                token,
            )
            if node := find_media(response, code):
                return node, page_url, code
            embed, _ = await self._read(page_url + "embed/captioned/")
            if node := page_media(embed.decode("utf-8", errors="replace"), code):
                return node, page_url, code
            if self.cookie:
                response = await self._optional_json(
                    f"https://i.instagram.com/api/v1/media/{media_id(code)}/info/"
                )
                if node := find_media(response, code):
                    return node, page_url, code
            raise DownloadError(
                "اینستاگرام داده رسانه نداد؛ پست ممکن است خصوصی، محدود یا نیازمند نشست مجاز باشد."
            )

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

    async def _graphql(self, url, doc_id, name, variables, page_url, token) -> dict:
        csrf = next((c.value for c in self.http.cookies.jar if c.name == "csrftoken"), "")
        if self.cookie:
            match = re.search(r"(?:^|;\s*)csrftoken=([^;]+)", self.cookie)
            if match:
                csrf = match[1]
        headers = {
            "X-FB-Friendly-Name": name,
            "X-FB-LSD": token,
            "X-CSRFToken": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": page_url,
            "Origin": "https://www.instagram.com",
            "Accept": "*/*",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        return await self._optional_json(
            url,
            method="POST",
            headers=headers,
            data={
                "lsd": token,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": name,
                "server_timestamps": "true",
                "variables": json.dumps(variables, separators=(",", ":")),
                "doc_id": doc_id,
            },
        )

    async def _optional_json(self, url: str, **kwargs) -> dict:
        try:
            data, _ = await self._read(url, **kwargs)
        except SiteHTTPError as error:
            if error.status not in {403, 404, 405, 410, 500, 502, 503, 504}:
                raise
            return {}
        except DownloadError as error:
            if not isinstance(error.__cause__, (httpx.HTTPError, TimeoutError)):
                raise
            return {}
        try:
            result = json.loads(data.decode("utf-8").removeprefix("for (;;);"))
            if not isinstance(result, dict):
                return {}
            message = str(result.get("message", "")).lower()
            if result.get("status") == "fail" and any(
                word in message
                for word in (
                    "please wait",
                    "login_required",
                    "challenge_required",
                    "checkpoint_required",
                )
            ):
                self._cooldown_until = time.monotonic() + 60
                raise SiteHTTPError(
                    401, "اینستاگرام محدودیت موقت یا الزام ورود اعمال کرده؛ کمی بعد امتحان کن."
                )
            return result
        except (ValueError, RecursionError):
            return {}

    async def sample(
        self, url: str, page_url: str, *, metadata: bool = False, tail: bool = False
    ) -> bytes:
        limit = 128 * 1024 if metadata else 1024
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
        if self.cookie and not cdn:
            request_headers["Cookie"] = self.cookie
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
            raise DownloadError("ارتباط با اینستاگرام قطع شد؛ دوباره امتحان کن.") from error
        raise DownloadError("تعداد تغییر مسیر لینک اینستاگرام بیش از حد است.")
