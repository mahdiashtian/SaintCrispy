import asyncio
from types import SimpleNamespace

import httpx
import pytest
from telethon import errors, functions, types

from downloader_bot.jobs import TransferJobs
from downloader_bot.models import Media, Quality, Source
from downloader_bot.progress import TransferProgress
from downloader_bot.telegram import TelegramDelivery

QUALITY = Quality("mp3_sq", "MP3", "mp3", None, "mp3", "audio/mpeg", "progressive", "endpoint")
MEDIA = Media("soundcloud", "track", "Title", "Artist", 30, "url", None, (QUALITY,))
PEER = types.InputPeerUser(1, 2)


async def test_managed_external_fetch_does_not_publish_until_download_is_registered():
    requests, published = [], []
    progress = TransferProgress()
    document = SimpleNamespace(id=7, access_hash=8, file_reference=b"ref")

    class Client:
        async def __call__(self, request):
            assert isinstance(request, functions.messages.UploadMediaRequest)
            assert isinstance(request.media, types.InputMediaDocumentExternal)
            assert progress.phase == "external" and not published
            requests.append(request)
            return SimpleNamespace(document=document)

        async def send_file(self, peer, media, **kwargs):
            assert progress.phase == "publishing"
            assert isinstance(media, types.InputMediaDocument)
            assert media.id.id == 7
            published.append(media)
            return SimpleNamespace(id=9, document=document)

    result = await TelegramDelivery(Client(), None, "ffmpeg").new_file(
        PEER,
        MEDIA,
        QUALITY,
        Source("https://cdn.example/audio.mp3", "progressive"),
        progress,
    )
    assert len(requests) == len(published) == 1
    assert result.document_id == 7 and result.origin_peer == bytes(PEER)


async def test_cancel_during_external_fetch_never_publishes_a_message():
    ready, cleaned = asyncio.Event(), asyncio.Event()

    class Client:
        async def __call__(self, request):
            assert isinstance(request, functions.messages.UploadMediaRequest)
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def send_file(self, *args, **kwargs):
            pytest.fail("Cancelling fetch must not publish a Telegram message")

    progress = TransferProgress()
    delivery = TelegramDelivery(Client(), None, "ffmpeg")
    async with TransferJobs() as jobs:
        job = jobs.reserve(1, 2, progress)
        jobs.start(
            job,
            lambda: delivery.new_file(
                PEER,
                MEDIA,
                QUALITY,
                Source("https://cdn.example/audio.mp3", "progressive"),
                progress,
            ),
        )
        await asyncio.wait_for(ready.wait(), 1)
        assert jobs.cancel(job.token, 1, 2)
        async with asyncio.timeout(1):
            await jobs.join()
    assert cleaned.is_set() and progress.phase == "cancelled"


async def test_failed_external_prefetch_falls_back_to_bounded_upload_without_publishing_twice():
    parts, messages = [], []
    document = SimpleNamespace(id=7, access_hash=8, file_reference=b"ref")
    progress = TransferProgress()

    class Client:
        async def __call__(self, request):
            if isinstance(request, functions.messages.UploadMediaRequest):
                raise errors.WebpageCurlFailedError(request=None)
            assert isinstance(request, functions.upload.SaveBigFilePartRequest)
            parts.append(request.bytes)
            return True

        async def send_file(self, peer, media, **kwargs):
            assert isinstance(media, types.InputMediaUploadedDocument)
            assert progress.phase == "publishing"
            messages.append(media)
            return SimpleNamespace(id=9, document=document)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"audio sample"),
        )
    ) as http:
        await TelegramDelivery(Client(), http, "ffmpeg").new_file(
            PEER,
            MEDIA,
            QUALITY,
            Source("https://cdn.example/audio.mp3", "progressive"),
            progress,
        )
    assert parts == [b"audio sample"] and len(messages) == 1
    assert progress.download_done and progress.upload_done


@pytest.mark.parametrize("failure", ["timeout", "different-size"])
async def test_unpublished_slow_or_wrong_url_result_streams_the_exact_origin_once(failure):
    body = b"exact original bytes"
    parts, published = [], []
    cancelled_fetch = asyncio.Event()
    document = SimpleNamespace(id=7, access_hash=8, file_reference=b"ref", size=len(body))

    class Client:
        async def __call__(self, request):
            if isinstance(request, functions.messages.UploadMediaRequest):
                if failure == "timeout":
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled_fetch.set()
                return SimpleNamespace(document=SimpleNamespace(size=len(body) + 3))
            assert isinstance(request, functions.upload.SaveBigFilePartRequest)
            parts.append(request.bytes)
            return True

        async def send_file(self, peer, media, **kwargs):
            assert isinstance(media, types.InputMediaUploadedDocument)
            published.append(media)
            return SimpleNamespace(id=9, document=document)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body),
        )
    ) as http:
        reference = await TelegramDelivery(Client(), http, "", external_timeout=0.01).new_file(
            PEER,
            MEDIA,
            QUALITY,
            Source("https://cdn.example/audio.mp3", "progressive", size_bytes=len(body)),
        )
    assert b"".join(parts) == body and len(published) == 1
    assert reference.size_bytes == len(body)
    assert cancelled_fetch.is_set() == (failure == "timeout")
