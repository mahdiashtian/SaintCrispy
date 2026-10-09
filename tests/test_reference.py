from types import SimpleNamespace

from telethon import errors, types

from downloader_bot.models import TelegramFile
from downloader_bot.telegram import TelegramDelivery


async def test_expired_reference_is_refreshed_from_telegram_without_downloading():
    origin = types.InputPeerUser(123, 456)
    stored = TelegramFile(7, 8, b"old", bytes(origin), 9)
    sent = []

    async def send_file(peer, media, **kwargs):
        sent.append(media)
        if len(sent) == 1:
            raise errors.FileReferenceExpiredError(request=None)

    async def get_messages(peer, ids):
        assert peer.user_id == 123 and ids == 9
        return SimpleNamespace(document=SimpleNamespace(id=7, access_hash=8, file_reference=b"new"))

    client = SimpleNamespace(send_file=send_file, get_messages=get_messages)
    delivery = TelegramDelivery(client, None, "ffmpeg")
    fresh = await delivery.resend("target", stored, "caption")
    assert fresh.file_reference == b"new"
    assert len(sent) == 2
    assert sent[1].id.file_reference == b"new"
