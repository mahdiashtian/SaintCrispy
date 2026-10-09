from types import SimpleNamespace

import httpx
import pytest
from telethon import errors, functions, types

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.schemas.media import Media, Quality, Source


@pytest.mark.parametrize(
    "mime,extension", [("video/mp4", "mp4"), ("image/jpeg", "jpg"), ("image/gif", "gif")]
)
async def test_pinterest_external_url_is_given_to_telegram_without_fetching_media(mime, extension):
    quality = Quality(
        "original", "Original", extension, None, extension, mime, "progressive", "url"
    )
    media = Media("pinterest", "123", "Demo", "Author", 0, "page", None, (quality,))
    url = f"https://i.pinimg.com/originals/demo.{extension}"
    peer = types.InputPeerUser(1, 2)

    class Client:
        async def __call__(self, request):
            assert isinstance(request, functions.messages.UploadMediaRequest)
            assert request.media.url == url
            return SimpleNamespace(
                document=SimpleNamespace(
                    id=4,
                    access_hash=5,
                    file_reference=b"reference",
                    attributes=[types.DocumentAttributeVideo(1, 32, 32, supports_streaming=True)],
                )
            )

        async def send_file(self, target, value, **kwargs):
            assert target == peer
            if mime == "video/mp4":
                assert isinstance(value, types.InputMediaDocument) and value.id.id == 4
            else:
                assert isinstance(value, types.InputMediaDocumentExternal) and value.url == url
            return SimpleNamespace(
                id=3, document=SimpleNamespace(id=4, access_hash=5, file_reference=b"reference")
            )

    def reject(_):
        pytest.fail("The bot must not fetch a full media file on the external delivery path")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        reference = await TelegramDelivery(Client(), http, "ffmpeg").new_file(
            peer, media, quality, Source(url, "progressive")
        )
    assert reference.document_id == 4 and reference.file_reference == b"reference"


async def test_image_fallback_uploads_the_original_as_a_reusable_document():
    body = b"GIF89a" + b"sample"
    quality = Quality("original", "Original", "gif", None, "gif", "image/gif", "progressive", "url")
    media = Media("pinterest", "123", "Demo", "Author", 0, "page", None, (quality,))
    uploaded = []

    class Client:
        async def __call__(self, request):
            assert request.bytes == body
            return True

        async def send_file(self, target, value, **kwargs):
            if isinstance(value, types.InputMediaDocumentExternal):
                raise errors.WebpageCurlFailedError(request=None)
            uploaded.append(value)
            return SimpleNamespace(
                id=3, document=SimpleNamespace(id=4, access_hash=5, file_reference=b"reference")
            )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    ) as http:
        reference = await TelegramDelivery(Client(), http, "ffmpeg").new_file(
            types.InputPeerUser(1, 2),
            media,
            quality,
            Source("https://i.pinimg.com/originals/demo.gif", "progressive"),
        )
    assert uploaded[0].force_file and uploaded[0].mime_type == "image/gif"
    assert reference.document_id == 4
