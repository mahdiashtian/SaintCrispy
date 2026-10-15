"""Apply consumer backpressure before libcurl fills an unbounded Python queue."""

import asyncio
from contextvars import ContextVar

from curl_cffi import Curl, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR, CURL_WRITEFUNC_PAUSE
from curl_cffi.requests import AsyncSession, Response

MAX_BUFFER = 256 * 1024
request_handles = ContextVar("curl_request_handles", default=None)


class ReceiveWindow:
    def __init__(self, curl):
        self.curl = curl
        self.buffered = 0
        self.peak = 0
        self.paused = False
        self.complete = False

    def write(self, callback, chunk):
        if len(chunk) > MAX_BUFFER:
            return CURL_WRITEFUNC_ERROR
        if self.buffered + len(chunk) > MAX_BUFFER:
            self.paused = True
            return CURL_WRITEFUNC_PAUSE
        result = callback(chunk)
        self.buffered += len(chunk)
        self.peak = max(self.peak, self.buffered)
        return result

    def consumed(self, size):
        self.buffered = max(0, self.buffered - size)
        if self.paused and not self.complete:
            self.paused = False
            self.curl.pause(0)


class BoundedCurl(Curl):
    def setopt(self, option, value):
        if option == CurlOpt.WRITEFUNCTION and callable(value):
            flow = self.flow = ReceiveWindow(self)
            callback = value

            def value(chunk):
                return flow.write(callback, chunk)

        return super().setopt(option, value)


class BoundedResponse(Response):
    def __init__(self, curl=None):
        super().__init__(curl)
        self.flow = getattr(curl, "flow", None)

    async def aiter_content(self, *args, **kwargs):
        try:
            async for chunk in super().aiter_content(*args, **kwargs):
                try:
                    yield chunk
                finally:
                    if self.flow:
                        self.flow.consumed(len(chunk))
        finally:
            await self.aclose()

    async def aclose(self):
        # The upstream close waits for EOF. Early probes/cancellation must stop I/O.
        if self.astream_task is not None:
            if not self.astream_task.done():
                self.astream_task.cancel()
            await asyncio.gather(self.astream_task, return_exceptions=True)


class BoundedSession(AsyncSession):
    def __init__(self, **options):
        super().__init__(response_class=BoundedResponse, **options)

    async def pop_curl(self):
        curl = await self.pool.get()
        curl = curl if curl is not None else BoundedCurl(debug=self.debug)
        if handles := request_handles.get():
            curl.lease = handles
            handles["handles"].append(curl)
        return curl

    async def _request_once(self, *args, **kwargs):
        lease = {"handles": [], "released": asyncio.Event()}
        token = request_handles.set(lease)
        try:
            return await super()._request_once(*args, **kwargs)
        except BaseException:
            for curl in lease["handles"]:
                if getattr(curl, "lease", None) is lease:
                    self.acurl.remove_handle(curl)
                    await asyncio.wait_for(lease["released"].wait(), 1)
            raise
        finally:
            request_handles.reset(token)

    def release_curl(self, curl):
        if flow := getattr(curl, "flow", None):
            flow.complete = True
        super().release_curl(curl)
        if lease := getattr(curl, "lease", None):
            curl.lease = None
            lease["released"].set()
