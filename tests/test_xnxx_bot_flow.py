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
from downloader_bot.downloaders.xnxx.client import XNXXClient
from downloader_bot.downloaders.xnxx.downloader import XNXXDownloader
from downloader_bot.services.download import DownloadService

PAGE_URL = "https://www.xnxx.com/video-demo/test"
PAGE = "<title>Demo</title>setVideoUrlHigh('https://cdn.example/video.mp4')"
MP4 = mp4_header() + b"x" * (600 * 1024)


@pytest.mark.parametrize("method", ["external", "stream", "failed_upload"])
async def test_registered_xnxx_handler_downloads_selected_quality_and_reuses_only_success(method):
    registrations, replies, edits, visits, uploads, sends = [], [], [], [], [], []
    stored = {}
    external_fetches = []
    peer = types.InputPeerUser(1, 2)

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
            return httpx.Response(200, content=MP4)
        assert str(request.url) == PAGE_URL
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
            DownloaderRouter({"xnxx": XNXXDownloader(XNXXClient(http))}),
            repository,
            TelegramDelivery(client, http, "ffmpeg"),
        )
        register_handlers(client, service, menus)
        message_handler, builder = next(
            (callback, event)
            for callback, event in registrations
            if getattr(callback, "func", callback).__name__ == "handle_xnxx_link"
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
        assert button.type.data.startswith(b"xnxx:")
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
        await callback_handler(selection)
        if method == "failed_upload":
            assert not stored
            assert (
                len(sends) == 0 and len(external_fetches) == 1
            )  # Registration failed; no publication.
            assert edits[-1].startswith("❌ دریافت فایل کامل نشد")
            return
        assert set(stored) == {("xnxx", "demo", "high")}
        assert stored["xnxx", "demo", "high"].document_id == 4
        assert isinstance(external_fetches[0], types.InputMediaDocumentExternal)
        if method == "stream":
            assert b"".join(uploads) == MP4
            assert isinstance(sends[0], types.InputMediaUploadedDocument)
            assert sends[0].mime_type == "video/mp4"
        else:
            assert not uploads
        assert "ارسال کامل شد" in edits[-1]
        visits.clear()
        sends.clear()
        uploads.clear()
        await callback_handler(selection)
        assert not visits and not uploads
        assert len(sends) == 1 and isinstance(sends[0], types.InputMediaDocument)
        assert "فایل ذخیره‌شده" in edits[-1]
