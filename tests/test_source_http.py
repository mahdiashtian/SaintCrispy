import asyncio
import hashlib
from contextlib import aclosing, asynccontextmanager, suppress

import httpx
import pytest

from core.bounded_curl import MAX_BUFFER, BoundedSession
from core.source_http import source_http
from schemas.media import Source


@asynccontextmanager
async def origin(size):
    tasks = set()
    payload = bytes(range(256)) * 256

    async def send(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\n\r\n".encode())
            for offset in range(0, size, len(payload)):
                writer.write(payload[: min(len(payload), size - offset)])
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(send, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/media", payload
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_slow_consumer_caps_python_queue_and_early_close_reaps_paused_transfer():
    async with origin(32 * 1024 * 1024) as (url, _):
        async with BoundedSession(trust_env=False, max_clients=1) as session:
            response = await session.get(url, stream=True, timeout=5)
            async with aclosing(response.aiter_content()) as chunks:
                assert await anext(chunks)
                await asyncio.sleep(0.05)
                assert response.flow.buffered <= MAX_BUFFER
                assert response.flow.peak <= MAX_BUFFER
                assert response.flow.paused
                await asyncio.wait_for(response.aclose(), 1)
            assert response.astream_task.done()
            assert response.flow.complete
            assert session.pool.qsize() == 1


async def test_pause_replay_preserves_every_byte_and_reused_handle_has_separate_flow():
    size = 6 * 1024 * 1024
    async with origin(size) as (url, payload):
        async with BoundedSession(trust_env=False, max_clients=1) as session:
            flows = []
            for _ in range(2):
                response = await session.get(url, stream=True, timeout=10)
                received, digest = 0, hashlib.sha256()
                async for chunk in response.aiter_content():
                    digest.update(chunk)
                    received += len(chunk)
                    await asyncio.sleep(0)
                assert received == size
                assert (
                    digest.hexdigest()
                    == hashlib.sha256(payload * (size // len(payload))).hexdigest()
                )
                assert response.flow.peak <= MAX_BUFFER
                flows.append(response.flow)
            assert flows[0] is not flows[1]


async def test_normal_sources_reuse_injected_session_without_creating_another_pool():
    async with httpx.AsyncClient() as http:
        async with source_http(
            http, Source("https://cdn.example/video", "progressive")
        ) as selected:
            assert selected is http
        assert not http.is_closed


async def test_cancellation_before_headers_removes_transfer_from_long_lived_session():
    entered = asyncio.Event()

    async def wait(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            entered.set()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(wait, "127.0.0.1", 0)
    try:
        async with BoundedSession(trust_env=False, max_clients=1) as session:
            request = asyncio.create_task(
                session.get(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/", stream=True)
            )
            await asyncio.wait_for(entered.wait(), 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 2)
            assert session.pool.qsize() == 1
    finally:
        server.close()
        await server.wait_closed()


async def test_doh_transfer_preserves_size_header_and_does_not_copy_origin_cookies(monkeypatch):
    from core import source_http as module

    visits, options = [], []

    class Browser:
        def __init__(self, **values):
            options.append(values)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def request(self, method, url, **values):
            visits.append(values)

            class Response:
                status_code = 200
                headers = httpx.Headers({"Content-Length": "4"})

                async def aiter_content(self):
                    yield b"data"

                async def aclose(self):
                    pass

            return Response()

    monkeypatch.setattr(module, "BoundedSession", Browser)
    async with httpx.AsyncClient(cookies={"session": "PRIVATE"}) as parent:
        source = Source("https://cdn.example/video", "progressive", http3=True)
        async with source_http(parent, source) as selected:
            response = await selected.get(source.url)
            assert response.content == b"data" and response.headers["content-length"] == "4"
    assert visits and "cookie" not in visits[0]["headers"]
    assert options[0]["discard_cookies"] and not options[0]["trust_env"]
