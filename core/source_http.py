"""Anonymous CDN transport selected by a resolved source's network profile."""

from contextlib import asynccontextmanager

import httpx
from curl_cffi import CurlHttpVersion, CurlOpt
from curl_cffi.requests.exceptions import RequestException

from core.bounded_curl import BoundedSession


class CurlStream(httpx.AsyncByteStream):
    def __init__(self, response):
        self.response = response

    async def __aiter__(self):
        try:
            async for chunk in self.response.aiter_content():
                yield chunk
        except RequestException as error:
            raise httpx.ReadError("CDN response interrupted") from error

    async def aclose(self):
        await self.response.aclose()


class CurlTransport(httpx.AsyncBaseTransport):
    def __init__(self, browser):
        self.browser = browser

    async def handle_async_request(self, request):
        try:
            response = await self.browser.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                data=(await request.aread()) or None,
                allow_redirects=False,
                stream=True,
                timeout=30,
            )
        except RequestException as error:
            raise httpx.ConnectError("CDN connection failed", request=request) from error
        # libcurl already decodes compressed bodies. Identity responses retain size.
        decoded = response.headers.get("content-encoding", "identity") != "identity"
        headers = [
            (key, value)
            for key, value in response.headers.multi_items()
            if not decoded or key.lower() not in {"content-encoding", "content-length"}
        ]
        return httpx.Response(response.status_code, headers=headers, stream=CurlStream(response))


@asynccontextmanager
async def source_http(http, source):
    if source.http3 or source.doh_url:
        options = {}
        if source.http3:
            options[CurlOpt.HTTP_VERSION] = CurlHttpVersion.V3ONLY
        if source.doh_url:
            options[CurlOpt.DOH_URL] = source.doh_url
        # No provider account jar or ambient proxy is copied into this CDN session.
        async with BoundedSession(
            impersonate="chrome",
            discard_cookies=True,
            trust_env=False,
            proxy=source.proxy or None,
            curl_options=options,
        ) as browser:
            async with httpx.AsyncClient(
                transport=CurlTransport(browser), trust_env=False, timeout=30
            ) as client:
                yield client
    elif source.proxy is not None:
        async with httpx.AsyncClient(
            proxy=source.proxy or None, trust_env=False, timeout=30
        ) as client:
            yield client
    else:
        yield http
