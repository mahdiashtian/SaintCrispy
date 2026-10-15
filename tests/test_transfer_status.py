import asyncio
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers.quality import run_transfer
from bot.premium_emoji import BotPresentation, PresentedMessage
from bot.progress import ProgressReporter, TransferProgress
from bot.state.states import ConversationState
from schemas.media import DownloadError, Media, Quality, Source, TelegramFile
from services.download import DownloadService


@pytest.mark.parametrize(
    "site", ["instagram", "youtube", "soundcloud", "pinterest", "xvideos", "xnxx"]
)
@pytest.mark.parametrize("method", ["progressive", "hls", "external", "reused"])
@pytest.mark.parametrize("chat_id", [7, -1001234])
async def test_delivered_file_replaces_stale_progress_without_repeating_delivery(
    monkeypatch, site, method, chat_id
):
    monkeypatch.setattr(
        "bot.handlers.quality.ProgressReporter", partial(ProgressReporter, interval=0.001)
    )
    monkeypatch.setattr(ProgressReporter, "RETRY_DELAY", 0)
    progress = TransferProgress()
    quality = Quality("full", "Full", "h264", None, "mp4", "video/mp4", "progressive", "endpoint")
    media = Media(site, "id", "Title", "Author", 30, "public-page", None, (quality,))
    reference = TelegramFile(1, 2, b"ref", b"peer", 3, 8_000_000, True, True, True, True)
    showing_progress = asyncio.Event()
    visible, edits, attempts = [], [], []

    async def edit(text, **options):
        edits.append((text, options))
        if progress.phase == "done":
            attempts.append(text)
            if len(attempts) == 1:
                raise OSError("temporary status connection failure")
        else:
            showing_progress.set()
        visible[:] = [text]

    async def new_file(peer, actual_media, selected, source, state):
        assert peer == chat_id and actual_media is media and selected is quality
        state.method = method
        state.phase = "external" if method == "external" else "streaming"
        # Both reported cases: known size at 65%, and HLS without a duration estimate.
        state.downloaded = reference.size_bytes
        state.total = reference.size_bytes if method == "progressive" else None
        state.uploaded = 5_000_000 if method == "progressive" else 0
        await asyncio.wait_for(showing_progress.wait(), 1)
        state.uploaded = reference.size_bytes
        state.download_done = state.upload_done = True
        return reference

    async def resend(peer, stored, caption, state):
        assert peer == chat_id and stored is reference
        await asyncio.wait_for(showing_progress.wait(), 1)
        return stored

    repository = SimpleNamespace(
        key=lambda *values: ":".join(values),
        get=AsyncMock(return_value=reference if method == "reused" else None),
        save=AsyncMock(),
        record_delivery=AsyncMock(),
    )
    delivery = SimpleNamespace(
        new_file=AsyncMock(side_effect=new_file), resend=AsyncMock(side_effect=resend)
    )
    service = DownloadService(
        SimpleNamespace(resolve=AsyncMock(return_value=Source("cdn", "progressive"))),
        repository,
        delivery,
    )

    async def finish(user, chat, transfer_id, state):
        assert visible == [progress.text()] and progress.phase == "done"
        assert state == ConversationState.DONE

    conversations = SimpleNamespace(finish_transfer=AsyncMock(side_effect=finish))
    presentation = BotPresentation()
    presentation.emojis = {"✅": 101}
    status = PresentedMessage(SimpleNamespace(edit=edit), presentation)
    event = SimpleNamespace(
        sender_id=7, chat_id=chat_id, get_input_chat=AsyncMock(return_value=chat_id)
    )
    menu = SimpleNamespace(media=media, message_id=42)
    await run_transfer(event, service, menu, 0, status, progress, conversations)

    assert len(attempts) == 2 and visible == [progress.text()]
    assert edits[-1][1]["buttons"].rows == []
    assert edits[-1][1]["formatting_entities"][0].document_id == 101
    assert delivery.new_file.await_count == (0 if method == "reused" else 1)
    assert delivery.resend.await_count == (1 if method == "reused" else 0)
    assert repository.save.await_count == (0 if method == "reused" else 1)
    assert repository.record_delivery.await_count == 1
    conversations.finish_transfer.assert_awaited_once()


@pytest.mark.parametrize("outcome", ["done", "error", "cancelled"])
async def test_terminal_status_precedes_slow_conversation_persistence(outcome):
    saved, release = asyncio.Event(), asyncio.Event()
    edits = []
    progress = TransferProgress()
    quality = Quality("full", "Full", "h264", None, "mp4", "video/mp4", "progressive", "url")
    media = Media("instagram", "id", "Title", "", 30, "url", None, (quality,))

    async def deliver(*args):
        if outcome == "error":
            raise DownloadError("دریافت فایل ممکن نشد.")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        # Returning successfully is authoritative even without a final progress callback.

    async def edit(text, **options):
        edits.append((text, options))

    async def finish(*args):
        assert progress.phase == outcome and edits[-1][0] == progress.text()
        assert (
            args[-1]
            == {
                "done": ConversationState.DONE,
                "error": ConversationState.FAILED,
                "cancelled": ConversationState.CANCELLED,
            }[outcome]
        )
        saved.set()
        await release.wait()

    event = SimpleNamespace(sender_id=1, chat_id=1, get_input_chat=AsyncMock(return_value=1))
    task = asyncio.create_task(
        run_transfer(
            event,
            SimpleNamespace(deliver=deliver),
            SimpleNamespace(media=media, message_id=42),
            0,
            SimpleNamespace(edit=edit),
            progress,
            SimpleNamespace(finish_transfer=finish),
        )
    )
    try:
        await asyncio.wait_for(saved.wait(), 1)
        assert not task.done()
        assert edits[-1][1]["buttons"].rows == []
    finally:
        release.set()
        results = await asyncio.gather(task, return_exceptions=True)
    if outcome == "cancelled":
        assert isinstance(results[0], asyncio.CancelledError)
    else:
        assert results == [None]
