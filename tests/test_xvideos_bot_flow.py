import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telethon import errors, functions, types
from video_fixture import mp4_header

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.handlers import register_handlers
from downloader_bot.bot.state.menus import MenuStore
from downloader_bot.downloaders.router import DownloaderRouter
from downloader_bot.downloaders.xvideos.client import XVideosClient
from downloader_bot.downloaders.xvideos.downloader import XVideosDownloader
from downloader_bot.services.download import DownloadService

PAGE_URL = "https://www.xvideos.com/video.demo/test"
PAGE = "<title>Demo</title>setVideoUrlHigh('https://cdn.example/video.mp4')"
MP4 = mp4_header() + b"x" * (600 * 1024)


@pytest.fixture
async def hls_origin(method, tmp_path):
    if method != "hls":
        yield None
        return
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    manifest = tmp_path / "video.m3u8"
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=32x32:r=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "hls",
        str(manifest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await process.communicate()
    assert process.returncode == 0, diagnostic.decode(errors="replace")
    files = {"/" + path.name: path.read_bytes() for path in tmp_path.iterdir()}
    files["/master.m3u8"] = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=32x32,"
        'CODECS="avc1.42e01e,mp4a.40.2"\nvideo.m3u8\n'
    ).encode()

    async def respond(reader, writer):
        try:
            async with asyncio.timeout(5):
                request = await reader.readuntil(b"\r\n\r\n")
                path = request.split(b" ")[1].decode().split("?", 1)[0]
                data = files[path]
                writer.write(
                    b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                    + str(len(data)).encode()
                    + b"\r\n\r\n"
                    + data
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield SimpleNamespace(url=f"http://127.0.0.1:{port}", files=files, ffmpeg=ffmpeg)


@pytest.mark.parametrize(
    "method", ["external", "stream", "failed_upload", "cancelled_upload", "hls"]
)
async def test_xvideos_menu_transfer_progress_and_success_only_cache(method, hls_origin):
    registrations, replies, edits, visits, uploads, sends = [], [], [], [], [], []
    stored = {}
    external_fetches = []
    peer = types.InputPeerUser(1, 2)
    upload_started = asyncio.Event()
    transfer_closed = asyncio.Event()

    class TransferBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield MP4

        async def aclose(self):
            transfer_closed.set()

    class Client:
        def add_event_handler(self, callback, event):
            registrations.append((callback, event))

        async def __call__(self, request):
            if isinstance(request, functions.messages.UploadMediaRequest):
                external_fetches.append(request.media)
                if method != "external":
                    raise errors.WebpageCurlFailedError(request=None)
                return SimpleNamespace(
                    document=SimpleNamespace(
                        id=4,
                        access_hash=5,
                        file_reference=b"reference",
                        size=len(MP4),
                        attributes=[
                            types.DocumentAttributeVideo(1, 32, 32, supports_streaming=True)
                        ],
                    )
                )
            uploads.append(request.bytes)
            if method == "cancelled_upload":
                upload_started.set()
                await asyncio.Event().wait()
            return method != "failed_upload"

        async def send_file(self, target, value, **kwargs):
            assert target == peer
            sends.append(value)
            if isinstance(value, types.InputMediaDocumentExternal) and method != "external":
                raise errors.WebpageCurlFailedError(request=None)
            return SimpleNamespace(
                id=3,
                document=SimpleNamespace(id=4, access_hash=5, file_reference=b"reference"),
            )

    async def get(site, identity, quality):
        return stored.get((site, identity, quality))

    async def save(site, identity, quality, reference):
        stored[site, identity, quality] = reference

    async def respond(text, **kwargs):
        replies.append((text, kwargs))
        return SimpleNamespace(edit=edit)

    async def edit(text, **kwargs):
        edits.append(text)

    async def answer(text, **kwargs):
        assert "شروع" in text

    async def get_input_chat():
        return peer

    def http_response(request):
        visits.append(str(request.url))
        if request.url.host == "cdn.example":
            assert request.headers["Referer"] == PAGE_URL
            if "range" not in request.headers:
                return httpx.Response(
                    200, stream=TransferBody(), headers={"Content-Length": str(len(MP4))}
                )
            return httpx.Response(200, content=MP4)
        if hls_origin and request.url.host == "127.0.0.1":
            return httpx.Response(200, content=hls_origin.files[request.url.path])
        assert str(request.url) == PAGE_URL
        if hls_origin:
            return httpx.Response(
                200, text=(f"setVideoHLS('{hls_origin.url}/master.m3u8'); setVideoDuration(1);")
            )
        return httpx.Response(200, text=PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(http_response)) as http:
        client, menus = Client(), MenuStore()
        repository = SimpleNamespace(
            key=lambda *args: "00000001",
            get=get,
            save=save,
            known_media=AsyncMock(return_value=None),
            remember_media=AsyncMock(),
            record_delivery=AsyncMock(),
        )
        service = DownloadService(
            DownloaderRouter({"xvideos": XVideosDownloader(XVideosClient(http))}),
            repository,
            TelegramDelivery(client, http, hls_origin.ffmpeg if hls_origin else "ffmpeg"),
        )
        register_handlers(client, service, menus)
        message_handler, builder = next(
            (callback, event)
            for callback, event in registrations
            if getattr(callback, "func", callback).__name__ == "handle_xvideos_link"
        )
        assert builder.pattern("لینک: " + PAGE_URL)
        await message_handler(
            SimpleNamespace(
                raw_text="لینک: " + PAGE_URL,
                sender_id=1,
                chat_id=1,
                respond=respond,
            )
        )
        button = replies[0][1]["buttons"][0][0]
        assert button.type.data.startswith(b"xv:")
        callback_handler, callback_builder = next(
            (callback, event)
            for callback, event in registrations
            if getattr(callback, "func", callback).__name__ == "handle_quality"
        )
        selection = SimpleNamespace(
            pattern_match=callback_builder.match(button.type.data),
            sender_id=1,
            chat_id=1,
            answer=answer,
            respond=respond,
            get_input_chat=get_input_chat,
        )
        if method == "cancelled_upload":
            transfer = asyncio.create_task(callback_handler(selection))
            try:
                await asyncio.wait_for(upload_started.wait(), 2)
                assert not transfer.done()  # Another coroutine ran while upload was awaiting I/O.
            finally:
                transfer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await transfer
            assert transfer_closed.is_set() and not stored and len(sends) == 0
            return
        await callback_handler(selection)
        if method == "failed_upload":
            assert not stored and len(sends) == 0
            assert edits[-1].startswith("❌ دریافت فایل کامل نشد")
            return
        quality_key = "hls_32x32_h264" if hls_origin else "high"
        assert set(stored) == {("xvideos", "demo", quality_key)}
        assert stored["xvideos", "demo", quality_key].document_id == 4
        if method == "hls":
            assert len(sends) == 1 and isinstance(sends[0], types.InputMediaUploadedDocument)
            video = next(
                item
                for item in sends[0].attributes
                if isinstance(item, types.DocumentAttributeVideo)
            )
            assert (video.w, video.h) == (32, 32)
            verify = await asyncio.create_subprocess_exec(
                hls_origin.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, diagnostic = await verify.communicate(b"".join(uploads))
            assert verify.returncode == 0, diagnostic.decode(errors="replace")
        else:
            assert isinstance(external_fetches[0], types.InputMediaDocumentExternal)
        if method == "stream":
            assert b"".join(uploads) == MP4
            assert isinstance(sends[0], types.InputMediaUploadedDocument)
            assert sends[0].mime_type == "video/mp4"
        elif method == "external":
            assert not uploads
        assert "ارسال کامل شد" in edits[-1]
        visits.clear()
        sends.clear()
        uploads.clear()
        await callback_handler(selection)
        assert not visits and not uploads
        assert len(sends) == 1 and isinstance(sends[0], types.InputMediaDocument)
        assert "فایل ذخیره‌شده" in edits[-1]
