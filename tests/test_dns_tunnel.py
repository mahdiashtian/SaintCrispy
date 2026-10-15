import asyncio
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

import httpx
import pytest

from core.dns_tunnel import DnsTunnel, configured_proxy, extractor_dns
from downloader_bot.downloaders.youtube.urls import network_host


def answer(address="1.1.1.1"):
    return {"Status": 0, "Answer": [{"type": 1, "TTL": 30, "data": address}]}


async def test_connect_relays_opaque_bytes_and_shuts_down(monkeypatch):
    async def echo(reader, writer):
        try:
            writer.write(await reader.read(100))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    real_connect = asyncio.open_connection
    calls = []

    async def connect(address, port):
        calls.append((address, port))
        return await real_connect("127.0.0.1", server.sockets[0].getsockname()[1])

    monkeypatch.setattr(asyncio, "open_connection", connect)
    try:
        async with DnsTunnel(
            "https://resolver.example/resolve",
            network_host,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=answer())),
        ) as tunnel:
            parsed = urlsplit(tunnel.proxy)
            reader, writer = await real_connect(parsed.hostname, parsed.port)
            writer.write(b"CONNECT www.youtube.com:443 HTTP/1.1\r\nHost: www.youtube.com\r\n\r\n")
            await writer.drain()
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            payload = b"\x16\x03\x03opaque-tls-payload"
            writer.write(payload)
            await writer.drain()
            assert await reader.readexactly(len(payload)) == payload
            writer.close()
            await writer.wait_closed()
        assert not tunnel.connections
        assert calls == [("1.1.1.1", 443)]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.10.34.35", "169.254.169.254", "::1", "192.0.2.1"]
)
async def test_private_or_reserved_dns_answers_are_never_connected(address):
    async with DnsTunnel(
        "https://resolver.example/resolve",
        network_host,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=answer(address))),
    ) as tunnel:
        with pytest.raises(OSError):
            await tunnel.addresses("www.youtube.com")


async def test_duplicate_dns_queries_are_coalesced_and_cancelled_follower_does_not_cancel_lookup():
    release, entered = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=answer())

    async with DnsTunnel(
        "https://resolver.example/resolve", network_host, transport=httpx.MockTransport(respond)
    ) as tunnel:
        tasks = [asyncio.create_task(tunnel.addresses("www.youtube.com")) for _ in range(100)]
        await entered.wait()
        tasks[0].cancel()
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1:] == [("1.1.1.1",)] * 99
        assert await tunnel.addresses("www.youtube.com") == ("1.1.1.1",)
        assert len(calls) == 1


@pytest.mark.parametrize("target", ["example.org:443", "127.0.0.1:443", "www.youtube.com:80"])
async def test_tunnel_rejects_other_hosts_and_ports_without_querying_dns(target):
    def respond(request):
        pytest.fail("A rejected target must not reach the DNS resolver")

    async with DnsTunnel(
        "https://resolver.example/resolve", network_host, transport=httpx.MockTransport(respond)
    ) as tunnel:
        parsed = urlsplit(tunnel.proxy)
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        writer.write(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 502")
        writer.close()
        await writer.wait_closed()


async def test_explicit_proxy_takes_precedence_and_disabled_profile_opens_no_server():
    async with AsyncExitStack() as stack:
        assert await configured_proxy(stack, {}, "YOUTUBE", network_host) == ""
        assert (
            await configured_proxy(
                stack,
                {"YOUTUBE_PROXY": "http://configured:8080", "YOUTUBE_DOH_URL": "invalid"},
                "YOUTUBE",
                network_host,
            )
            == "http://configured:8080"
        )


@pytest.mark.parametrize(
    "data", [[], {"Status": 2}, {"Status": 0, "Answer": {}}, {"Status": 0, "Answer": []}]
)
async def test_invalid_dns_payload_is_a_controlled_failure(data):
    async with DnsTunnel(
        "https://resolver.example/resolve",
        network_host,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)),
    ) as tunnel:
        with pytest.raises(OSError):
            await tunnel.addresses("www.youtube.com")


async def test_oversized_dns_response_is_rejected_before_parsing():
    async with DnsTunnel(
        "https://resolver.example/resolve",
        network_host,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b" " * 65537)),
    ) as tunnel:
        with pytest.raises(OSError, match="exceeds limit"):
            await tunnel.addresses("www.youtube.com")


@pytest.mark.parametrize(
    "resolver",
    ["http://resolver.example", "https://user:SECRET@dns.test", "https://dns.test/#fragment"],
)
def test_resolver_validation_rejects_insecure_or_credential_urls_without_echoing_them(resolver):
    with pytest.raises(ValueError) as failure:
        DnsTunnel(resolver, network_host)
    assert "SECRET" not in str(failure.value)


async def test_failed_resolution_cools_down_instead_of_requerying_every_request():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"Status": 2})

    async with DnsTunnel(
        "https://resolver.example/resolve", network_host, transport=httpx.MockTransport(respond)
    ) as tunnel:
        for _ in range(100):
            with pytest.raises(OSError):
                await tunnel.addresses("www.youtube.com")
        assert len(calls) == 1


def test_subprocess_dns_context_restores_global_resolver_and_bounds_scope(monkeypatch):
    import socket

    original_client = httpx.Client
    original_resolver = socket.getaddrinfo
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=answer())

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(RuntimeError):
        with extractor_dns("https://resolver.example/resolve", network_host):
            assert socket.getaddrinfo("www.youtube.com", 443)[0][4][0] == "1.1.1.1"
            assert socket.getaddrinfo("www.youtube.com", 443)[0][4][0] == "1.1.1.1"
            socket.getaddrinfo("localhost", 443)
            assert len(calls) == 1
            raise RuntimeError("worker stopped")
    assert socket.getaddrinfo is original_resolver
