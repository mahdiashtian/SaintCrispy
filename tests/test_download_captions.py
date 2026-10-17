from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, types

from bot.delivery import TelegramDelivery
from bot.premium_emoji import BotPresentation
from bot.texts.downloads import DOWNLOAD_COPY, download_caption, quality_menu_text
from core.request_context import user_request
from schemas.media import Media, Quality, Source, TelegramFile
from services.download import DownloadService

QUALITY = Quality("720p", "720p · MP4", "h264", None, "mp4", "video/mp4", "progressive", "720")
FILE = TelegramFile(
    1,
    2,
    b"ref",
    b"peer",
    3,
    size_bytes=100,
    verified_complete=True,
    video_streaming=True,
    video_preview=True,
    video_faststart=True,
)


def media(site, title="Title"):
    return Media(
        site, "identity", title, "@creator", 12, "https://example.com/post", None, (QUALITY,)
    )


@pytest.mark.parametrize("site", tuple(DOWNLOAD_COPY))
def test_captions_and_photo_menus_fit_telegram_utf16_limit_with_large_metadata(site):
    item = media(site, "🎬" * 1500)
    for text in (download_caption(item, QUALITY, "@ExampleBot"), quality_menu_text(item)):
        assert len(text.encode("utf-16-le")) // 2 <= 1024
        assert "\ud83c" not in text
    assert "@ExampleBot" in download_caption(item, QUALITY, "https://t.me/ExampleBot")


def test_instagram_copy_omits_source_advertising_and_formats_creator_once():
    item = media("instagram", "Follow us for more!🧡")
    for text in (download_caption(item, QUALITY), quality_menu_text(item)):
        assert "Follow us" not in text and "🎧" not in text
        assert "@creator" in text and "@@creator" not in text


@pytest.mark.parametrize("site", tuple(DOWNLOAD_COPY))
@pytest.mark.parametrize("chat", [7, -1000000000123])
async def test_fresh_file_and_cached_resend_have_identical_copy_and_premium_entities(
    monkeypatch, site, chat
):
    sent = []

    async def send_file(peer, file, **options):
        sent.append(options)

    presentation = BotPresentation()
    presentation.emojis = {DOWNLOAD_COPY[site].icon: 101, "⚙️": 102, "💬": 103}
    delivery = TelegramDelivery(
        SimpleNamespace(send_file=send_file),
        None,
        "ffmpeg",
        presentation=presentation,
        bot_username="ExampleBot",
    )

    async def external(peer, source, caption, size, progress, quality):
        await delivery._send_file(peer, "uploaded", caption=caption, parse_mode=None)
        return FILE

    monkeypatch.setattr(delivery, "_try_external", external)
    repository = SimpleNamespace(
        key=lambda *args: "key",
        get=AsyncMock(side_effect=[None, None, FILE]),
        save=AsyncMock(),
        record_delivery=AsyncMock(),
    )
    service = DownloadService(
        SimpleNamespace(
            resolve=AsyncMock(
                return_value=Source("https://cdn.example/video", "progressive", size_bytes=100)
            )
        ),
        repository,
        delivery,
        caption_builder=partial(download_caption, bot_username="ExampleBot"),
    )
    item = media(site, "**literal title**")
    peer = types.InputPeerUser(7, 0) if chat > 0 else types.InputPeerChannel(123, 0)
    with user_request(7, chat, message_id=42):
        assert await service.deliver(peer, item, QUALITY) == "transferred"
        assert await service.deliver(peer, item, QUALITY) == "reused"
    assert sent[0]["caption"] == sent[1]["caption"]
    assert repository.record_delivery.await_count == 2
    for options in sent:
        assert options["reply_to"] == 42
        assert options["parse_mode"] is None
        entities = options["formatting_entities"]
        expected = [101, 101, 102, 103] if site == "xvideos" else [101, 102, 103]
        assert [e.document_id for e in entities] == expected
        encoded = options["caption"].encode("utf-16-le")
        assert (
            encoded[entities[0].offset * 2 : (entities[0].offset + entities[0].length) * 2].decode(
                "utf-16-le"
            )
            == DOWNLOAD_COPY[site].icon
        )


async def test_caption_premium_rejection_retries_once_as_unicode_without_losing_caption():
    sent = []

    async def send_file(peer, file, **options):
        sent.append(options)
        if "formatting_entities" in options:
            raise errors.PremiumAccountRequiredError(request=None)

    presentation = BotPresentation()
    presentation.emojis = {"📸": 101}
    delivery = TelegramDelivery(
        SimpleNamespace(send_file=send_file), None, "ffmpeg", presentation=presentation
    )
    text = download_caption(media("instagram"), QUALITY)
    await delivery.resend("peer", FILE, text)
    await delivery.resend("peer", FILE, text)
    assert len(sent) == 3 and presentation.disabled
    assert all(options["caption"] == text for options in sent)
    assert all("formatting_entities" not in options for options in sent[1:])
