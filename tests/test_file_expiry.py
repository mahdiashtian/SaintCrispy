"""File-only deadlines, restart recovery and bounded deletion without Telegram RPCs."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, types, utils

from bot.delivery import TelegramDelivery
from bot.file_expiry import FileExpiry
from bot.texts.downloads import (
    DOWNLOAD_COPY,
    FILE_EXPIRY_NOTICE,
    download_caption,
    inspection_text,
    quality_menu_text,
)
from core.request_context import user_request
from schemas.media import Media, Quality, TelegramFile

PEER = types.InputPeerUser(7, 123)
QUALITY = Quality("original", "Original", "mp4", None, "mp4", "video/mp4", "progressive", "")


class Deadlines:
    def __init__(self, clock):
        self.clock, self.rows = clock, {}
        self.offline = False

    async def add(self, chat_id, message_id, peer, delete_at):
        if self.offline:
            raise OSError("storage unavailable")
        self.rows.setdefault(
            (chat_id, message_id),
            dict(
                chat_id=chat_id,
                message_id=message_id,
                peer=peer,
                delete_at=delete_at,
                next_attempt_at=delete_at,
                attempts=0,
                completed=False,
            ),
        )

    async def due(self):
        if self.offline:
            raise OSError("storage unavailable")
        return [
            r.copy()
            for r in self.rows.values()
            if not r["completed"] and r["next_attempt_at"] <= self.clock()
        ][:100]

    async def finish(self, chat_id, message_id):
        if self.offline:
            raise OSError("storage unavailable")
        self.rows[chat_id, message_id]["completed"] = True

    async def defer(self, chat_id, message_id, seconds):
        if self.offline:
            raise OSError("storage unavailable")
        row = self.rows[chat_id, message_id]
        row["attempts"] += 1
        row["next_attempt_at"] = self.clock() + timedelta(seconds=seconds)


def cleanup_fixture():
    clock = [datetime(2026, 10, 15, tzinfo=UTC)]
    repository = Deadlines(lambda: clock[0])
    client = SimpleNamespace(delete_messages=AsyncMock())
    files = SimpleNamespace(forget_origin=AsyncMock())
    worker = FileExpiry(client, repository, files, clock=lambda: clock[0])
    return clock, repository, client, files, worker


@pytest.mark.parametrize("peer", [PEER, types.InputPeerChat(123), types.InputPeerChannel(123, 456)])
async def test_only_sent_file_id_expires_after_30_seconds_and_recovers_after_restart(peer):
    clock, repository, client, files, worker = cleanup_fixture()
    await worker.schedule(peer, SimpleNamespace(id=901))
    chat = utils.get_peer_id(peer)
    deadline = repository.rows[chat, 901]["delete_at"]
    assert deadline == clock[0] + timedelta(seconds=30)
    clock[0] += timedelta(seconds=29)
    await worker.run_once()
    client.delete_messages.assert_not_awaited()
    restored = FileExpiry(client, repository, files, clock=lambda: clock[0])
    clock[0] += timedelta(seconds=1)
    await restored.run_once()
    client.delete_messages.assert_awaited_once_with(peer, [901], revoke=True)
    files.forget_origin.assert_awaited_once_with(bytes(peer), 901)
    assert repository.rows[chat, 901]["completed"]
    assert repository.rows[chat, 901]["delete_at"] == deadline


@pytest.mark.parametrize("kind", ["video", "audio", "photo", "document", "cached"])
async def test_user_file_send_schedules_after_confirmation_and_preserves_link_reply(kind):
    clock, repository, client, files, worker = cleanup_fixture()
    sent = SimpleNamespace(id=901)

    async def send_file(peer, media, **options):
        assert not repository.rows
        assert options["reply_to"] == 42
        return sent

    delivery = TelegramDelivery(
        SimpleNamespace(send_file=send_file), None, "ffmpeg", file_expiry=worker
    )
    with user_request(7, 7, message_id=42):
        assert await delivery._send_file(PEER, kind, caption="caption") is sent
    assert set(repository.rows) == {(7, 901)}
    assert (7, 42) not in repository.rows
    await worker.run_once()
    client.delete_messages.assert_not_awaited()


async def test_storage_channel_and_unconfirmed_or_rejected_sends_are_not_deleted():
    _, repository, _, _, worker = cleanup_fixture()
    send_file = AsyncMock(return_value=SimpleNamespace(id=901))
    delivery = TelegramDelivery(
        SimpleNamespace(send_file=send_file), None, "ffmpeg", file_expiry=worker
    )
    with user_request(7, 7, message_id=42):
        await delivery._send_file(
            types.InputPeerChannel(100, 123), b"storage", caption="caption\n\n" + FILE_EXPIRY_NOTICE
        )
        assert send_file.call_args.kwargs["caption"] == "caption"
        send_file.side_effect = errors.ChatWriteForbiddenError(None)
        with pytest.raises(errors.ChatWriteForbiddenError):
            await delivery._send_file(PEER, b"file", caption="caption")
    await worker.schedule(PEER, None)
    await worker.schedule(PEER, SimpleNamespace(id=True))
    assert not repository.rows and not worker._pending


@pytest.mark.parametrize("site", DOWNLOAD_COPY)
@pytest.mark.parametrize("peer", [PEER, types.InputPeerChannel(123, 456)])
async def test_actual_cached_resends_get_new_deadlines_and_keep_the_save_caption(site, peer):
    clock, repository, _, _, worker = cleanup_fixture()
    sent = SimpleNamespace(id=902)
    client = SimpleNamespace(send_file=AsyncMock(return_value=sent))
    delivery = TelegramDelivery(client, None, "ffmpeg", file_expiry=worker)
    cached = TelegramFile(1, 2, b"ref", bytes(PEER), 901, 100, True, True, True, True)
    media = Media(site, "id", "Title", "Author", 30, "url", None, (QUALITY,))
    caption = download_caption(media, QUALITY)
    chat = utils.get_peer_id(peer)
    with user_request(7, chat, message_id=42):
        assert await delivery.resend(peer, cached, caption) == cached
    assert repository.rows[chat, 902]["delete_at"] == clock[0] + timedelta(seconds=30)
    assert client.send_file.call_args.kwargs["reply_to"] == 42
    assert FILE_EXPIRY_NOTICE in client.send_file.call_args.kwargs["caption"]
    assert (chat, 901) not in repository.rows and (chat, 42) not in repository.rows


async def test_lost_delete_permission_keeps_retry_but_does_not_forget_the_live_file():
    clock, repository, client, files, worker = cleanup_fixture()
    await worker.schedule(PEER, SimpleNamespace(id=901))
    clock[0] += timedelta(seconds=30)
    client.delete_messages.side_effect = errors.ChatAdminRequiredError(None)
    await worker.run_once()
    assert not repository.rows[7, 901]["completed"]
    files.forget_origin.assert_not_awaited()
    client.delete_messages.side_effect = None
    clock[0] += timedelta(seconds=3)
    await worker.run_once()
    assert repository.rows[7, 901]["completed"]


async def test_cancellation_after_publication_preserves_pending_deadline_for_the_worker():
    clock, repository, client, files, worker = cleanup_fixture()
    entered = asyncio.Event()
    original = repository.add

    async def blocked(*args):
        entered.set()
        await asyncio.Future()

    repository.add = blocked
    task = asyncio.create_task(worker.schedule(PEER, SimpleNamespace(id=901)))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker._pending
    repository.add = original
    await worker._flush()
    assert repository.rows[7, 901]["delete_at"] == clock[0] + timedelta(seconds=30)


async def test_database_failure_does_not_repeat_publication_and_recovers_pending_cleanup():
    clock, repository, client, files, worker = cleanup_fixture()
    repository.offline = True
    send_file = AsyncMock(return_value=SimpleNamespace(id=901))
    delivery = TelegramDelivery(
        SimpleNamespace(send_file=send_file), None, "ffmpeg", file_expiry=worker
    )
    with user_request(7, 7, message_id=42):
        await delivery._send_file(PEER, b"file", caption="caption")
    send_file.assert_awaited_once()
    clock[0] += timedelta(seconds=30)
    await worker.run_once()
    client.delete_messages.assert_awaited_once()
    assert worker._pending
    repository.offline = False
    await worker._flush()
    clock[0] += timedelta(seconds=3)
    await worker.run_once()
    client.delete_messages.assert_awaited_once()
    assert repository.rows[7, 901]["completed"]
    send_file.assert_awaited_once()


async def test_flood_wait_defers_cleanup_without_renewing_the_original_lifetime(monkeypatch):
    clock, repository, client, files, worker = cleanup_fixture()
    timer = [100.0]
    monkeypatch.setattr("bot.file_expiry.time.monotonic", lambda: timer[0])
    await worker.schedule(PEER, SimpleNamespace(id=901))
    deadline = repository.rows[7, 901]["delete_at"]
    clock[0] += timedelta(seconds=30)
    client.delete_messages.side_effect = errors.FloodWaitError(None, capture=10)
    await worker.run_once()
    assert repository.rows[7, 901]["next_attempt_at"] == clock[0] + timedelta(seconds=10)
    await worker.run_once()
    client.delete_messages.assert_awaited_once()
    timer[0] += 11
    clock[0] += timedelta(seconds=11)
    client.delete_messages.side_effect = None
    await worker.run_once()
    assert client.delete_messages.await_count == 2
    assert repository.rows[7, 901]["delete_at"] == deadline


async def test_many_expirations_batch_by_peer_and_worker_shutdown_leaves_durable_deadlines():
    clock, repository, client, files, worker = cleanup_fixture()
    for identity in range(1, 251):
        await worker.schedule(PEER, SimpleNamespace(id=identity))
    clock[0] += timedelta(seconds=30)
    async with worker:
        for _ in range(100):
            if all(row["completed"] for row in repository.rows.values()):
                break
            worker._wake.set()
            await asyncio.sleep(0.01)
    assert all(row["completed"] for row in repository.rows.values())
    assert worker._task.done()
    assert [len(call.args[1]) for call in client.delete_messages.await_args_list] == [100, 100, 50]
    assert files.forget_origin.await_count == 250


@pytest.mark.parametrize("site", DOWNLOAD_COPY)
def test_only_final_file_captions_include_save_warning(site):
    media = Media(site, "id", "Title", "Author", 30, "url", None, (QUALITY,))
    assert FILE_EXPIRY_NOTICE in download_caption(media, QUALITY)
    assert FILE_EXPIRY_NOTICE not in quality_menu_text(media)
    assert FILE_EXPIRY_NOTICE not in inspection_text(site)
