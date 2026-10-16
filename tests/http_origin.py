"""Serve neutral generated fixtures over HTTP, matching the remote-media boundary."""

import asyncio
import re
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlsplit


@asynccontextmanager
async def serve_directory(directory):
    directory = directory.resolve()

    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            name = unquote(urlsplit(request.split(b" ")[1].decode()).path).lstrip("/")
            path = (directory / name).resolve()
            if not path.is_relative_to(directory) or not path.is_file():
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            else:
                body = path.read_bytes()
                status, extra = "200 OK", ""
                if match := re.search(rb"(?im)^range: bytes=(\d+)-(\d*)\r?$", request):
                    start = int(match[1])
                    end = min(int(match[2]) if match[2] else len(body) - 1, len(body) - 1)
                    extra = f"Content-Range: bytes {start}-{end}/{len(body)}\r\n"
                    body, status = body[start : end + 1], "206 Partial Content"
                writer.write(
                    f"HTTP/1.1 {status}\r\n{extra}Content-Length: {len(body)}\r\nAccept-Ranges: bytes\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
