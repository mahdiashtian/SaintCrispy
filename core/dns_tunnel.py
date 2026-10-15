"""Optional loopback CONNECT transport with public DNS-over-HTTPS resolution.

TLS stays end-to-end. Only composition-supplied provider hosts on port 443 pass.
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from collections import OrderedDict
from contextlib import contextmanager, suppress
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)


def public_dns_answers(body):
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("Status") != 0:
        raise OSError("DNS query failed")
    addresses, ttl = [], 300
    records = data.get("Answer")
    if not isinstance(records, list):
        raise OSError("Invalid DNS answer collection")
    for record in records[:64]:
        if not isinstance(record, dict) or record.get("type") != 1:
            continue
        try:
            address = ipaddress.ip_address(record.get("data", ""))
        except ValueError:
            continue
        if address.version != 4 or not address.is_global:
            continue
        addresses.append(str(address))
        value = record.get("TTL")
        if type(value) is int:
            ttl = min(ttl, max(1, value))
    if not addresses:
        raise OSError("No public DNS address available")
    result = tuple(dict.fromkeys(addresses))[:4]
    return result, ttl


class DnsTunnel:
    def __init__(self, resolver, allowed_host, *, transport=None):
        parsed = urlsplit(resolver)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("DNS resolver must be an HTTPS URL without credentials or fragment")
        self.resolver, self.allowed_host = resolver, allowed_host
        self.transport = transport
        self.http = None
        self.server = None
        self.proxy = ""
        self.cache = OrderedDict()
        self.lookups = {}
        self.connections = set()

    async def __aenter__(self):
        self.http = httpx.AsyncClient(transport=self.transport, trust_env=False, timeout=8)
        try:
            self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=8192)
        except BaseException:
            await self.http.aclose()
            raise
        self.proxy = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        tasks = [*self.connections, *self.lookups.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.connections.clear()
        self.lookups.clear()
        await self.http.aclose()

    def _accept(self, reader, writer):
        if len(self.connections) >= 2048:
            writer.close()
            return
        task = asyncio.create_task(self._connect(reader, writer))
        self.connections.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task):
        self.connections.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error({"event": "dns_tunnel_failure", "error_type": type(error).__name__})

    async def addresses(self, host):
        entry = self.cache.get(host)
        if entry and entry[0] > time.monotonic():
            self.cache.move_to_end(host)
            if entry[1] is None:
                raise OSError("DNS resolution is cooling down")
            return entry[1]
        task = self.lookups.get(host)
        if task is None:
            if len(self.lookups) >= 64:
                raise OSError("DNS lookup capacity reached")
            task = self.lookups[host] = asyncio.create_task(self._lookup(host))

            def done(finished):
                self.lookups.pop(host, None)
                # A disconnected follower must not leave an unobserved task error.
                if not finished.cancelled():
                    if finished.exception() is not None:
                        self.cache[host] = time.monotonic() + 5, None
                        while len(self.cache) > 4096:
                            self.cache.popitem(last=False)

            task.add_done_callback(done)
        return await asyncio.shield(task)

    async def _lookup(self, host):
        async with self.http.stream(
            "GET",
            self.resolver,
            params={"name": host, "type": "A"},
            headers={"Accept": "application/dns-json"},
        ) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(8192):
                body.extend(chunk)
                if len(body) > 65536:
                    raise OSError("DNS response exceeds limit")
        result, ttl = public_dns_answers(body)
        self.cache[host] = time.monotonic() + ttl, result
        self.cache.move_to_end(host)
        while len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return result

    async def _connect(self, reader, writer):
        upstream = None
        pumps = []
        connected = False
        try:
            async with asyncio.timeout(20):
                header = await reader.readuntil(b"\r\n\r\n")
                if len(header) > 8192:
                    raise OSError("CONNECT header exceeds limit")
                line = header.split(b"\r\n", 1)[0].decode("ascii")
                match = re.fullmatch(r"CONNECT ([A-Za-z0-9.-]{1,253}):443 HTTP/1\.[01]", line)
                if not match or not self.allowed_host(host := match[1].lower()):
                    raise OSError("CONNECT target is not allowed")
                for address in await self.addresses(host):
                    try:
                        async with asyncio.timeout(5):
                            remote, upstream = await asyncio.open_connection(address, 443)
                        break
                    except OSError:
                        continue
                if upstream is None:
                    raise OSError("Origin connection failed")
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                connected = True

            async def copy(source, target):
                while True:
                    async with asyncio.timeout(90):
                        chunk = await source.read(32768)
                        if not chunk:
                            with suppress(OSError):
                                target.write_eof()
                            return
                        target.write(chunk)
                        await target.drain()

            pumps = [
                asyncio.create_task(copy(reader, upstream)),
                asyncio.create_task(copy(remote, writer)),
            ]
            await asyncio.gather(*pumps)
        except (
            OSError,
            TimeoutError,
            ValueError,
            httpx.HTTPError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            if not connected:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                with suppress(OSError):
                    await writer.drain()
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()


async def configured_proxy(stack, values, prefix, allowed_host):
    proxy = values.get(prefix + "_PROXY") or ""
    resolver = values.get(prefix + "_DOH_URL") or ""
    if proxy or not resolver:
        return proxy
    tunnel = await stack.enter_async_context(DnsTunnel(resolver, allowed_host))
    return tunnel.proxy


@contextmanager
def extractor_dns(resolver, allowed_host):
    """Resolve only a subprocess extractor's provider hosts; never patch the bot loop."""
    DnsTunnel(resolver, allowed_host)  # Validate the same resolver contract.
    resolver_host = urlsplit(resolver).hostname
    original = socket.getaddrinfo
    cache = OrderedDict()
    with httpx.Client(trust_env=False, timeout=8) as http:

        def addresses(host, port, family=0, type=0, proto=0, flags=0):
            name = host.decode("ascii") if isinstance(host, bytes) else host
            if name == resolver_host or not isinstance(name, str) or not allowed_host(name.lower()):
                return original(host, port, family, type, proto, flags)
            entry = cache.get(name)
            if entry is None or entry[0] <= time.monotonic():
                with http.stream(
                    "GET",
                    resolver,
                    params={"name": name, "type": "A"},
                    headers={"Accept": "application/dns-json"},
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes(8192):
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise OSError("DNS response exceeds limit")
                ips, ttl = public_dns_answers(body)
                entry = cache[name] = (time.monotonic() + ttl, ips)
                while len(cache) > 256:
                    cache.popitem(last=False)
            return [
                item
                for ip in entry[1]
                for item in original(ip, port, socket.AF_INET, type, proto, flags)
            ]

        socket.getaddrinfo = addresses
        try:
            yield
        finally:
            socket.getaddrinfo = original
