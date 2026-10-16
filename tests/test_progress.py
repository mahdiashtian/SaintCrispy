import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import StringSession

from bot.progress import ProgressReporter, TransferProgress
from bot.transfers.streaming import PART_SIZE, media_chunks, upload_stream
from schemas.media import DownloadError, Quality, Source

QUALITY = Quality("original", "Original", "mp3", None, "mp3", "audio/mpeg", "progressive", "url")


@pytest.mark.parametrize("phase", ["done", "error", "cancelled"])
async def test_terminal_status_uses_real_telethon_edit_without_an_empty_keyboard(phase):
    requests = []

    class Client(TelegramClient):
        async def get_input_entity(self, peer):
            return peer

        async def __call__(self, request, *args, **options):
            assert isinstance(request, functions.messages.EditMessageRequest)
            bytes(request)
            requests.append(request)
            if (
                isinstance(request.reply_markup, types.ReplyInlineMarkup)
                and not request.reply_markup.rows
            ):
                raise errors.ReplyMarkupInvalidError(request)
            return None

        def _get_response_message(self, *args):
            return None

    client = Client(StringSession(), 123, "test")
    peer = types.InputPeerUser(7, 123)

    async def edit(text, **options):
        await client.edit_message(peer, 901, text, **options)

    status = SimpleNamespace(edit=edit, delete=AsyncMock())
    progress = TransferProgress(error="No matching audio")
    async with ProgressReporter(status, progress, clear_buttons_on_exit=True) as reporter:
        progress.phase = phase
    assert len(requests) == 1
    assert requests[0].reply_markup is None
    assert requests[0].message == progress.text()
    assert reporter._buttons_cleared
    status.delete.assert_not_awaited()


@pytest.mark.parametrize("phase", ["done", "error", "cancelled"])
async def test_uneditable_status_gets_a_confirmed_terminal_reply_before_cleanup(phase):
    calls = []

    async def edit(*args, **options):
        raise errors.ReplyMarkupInvalidError(None)

    async def reply(text):
        calls.append(("reply", text))

    async def delete():
        calls.append(("delete", None))

    progress = TransferProgress(error="No matching audio")
    async with ProgressReporter(
        SimpleNamespace(edit=edit, delete=delete), progress, terminal_reply=reply
    ):
        progress.phase = phase
    assert calls == [("reply", progress.text()), ("delete", None)]


@pytest.mark.parametrize("failure", ["timeout", "permission"])
async def test_unconfirmed_terminal_reply_is_not_repeated_and_keeps_original_status(
    failure, monkeypatch
):
    async def edit(*args, **options):
        raise errors.ReplyMarkupInvalidError(None)

    async def reply(text):
        if failure == "timeout":
            await asyncio.Future()
        raise errors.ChatWriteForbiddenError(None)

    monkeypatch.setattr(ProgressReporter, "EDIT_TIMEOUT", 0.01)
    reply = AsyncMock(side_effect=reply)
    status = SimpleNamespace(edit=edit, delete=AsyncMock())
    progress = TransferProgress(error="No matching audio")
    async with ProgressReporter(status, progress, terminal_reply=reply) as reporter:
        progress.phase = "error"
    reply.assert_awaited_once_with(progress.text())
    status.delete.assert_not_awaited()
    assert reporter._task.done()


async def test_known_length_counts_download_and_only_confirmed_upload_bytes():
    data = b"x" * (PART_SIZE + 17)
    progress = TransferProgress(phase="streaming", method="progressive")
    observations = []

    async def client(request):
        observations.append((progress.downloaded, progress.uploaded, progress.text()))
        return True

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=data))
    async with httpx.AsyncClient(transport=transport) as http:
        chunks = media_chunks(
            http, Source("https://cdn.example/audio", "progressive"), QUALITY, "", progress
        )
        await upload_stream(client, chunks, "audio.mp3", progress=progress)

    assert progress.total == len(data)
    assert progress.downloaded == progress.uploaded == len(data)
    assert progress.download_done and progress.upload_done
    assert observations[0][1] == 0  # Sending a part does not yet mean Telegram accepted it.
    assert observations[-1][1] == PART_SIZE
    assert "دانلود: 100٪" in progress.text()
    assert "آپلود: 100٪" in progress.text()


async def test_failed_part_does_not_advance_upload_counter():
    progress = TransferProgress()

    async def chunks():
        yield b"x" * PART_SIZE

    async def reject(request):
        return False

    with pytest.raises(DownloadError):
        await upload_stream(reject, chunks(), "broken.mp3", progress=progress)
    assert progress.uploaded == 0
    assert not progress.upload_done


def test_hls_marks_estimate_and_caps_progress_until_actual_completion():
    progress = TransferProgress(
        phase="streaming",
        method="hls",
        duration=100,
        seconds=120,
        estimated_total=1000,
        uploaded=1200,
    )
    text = progress.text()
    assert "دانلود: 99٪" in text
    assert "آپلود: 99٪ (حدودی)" in text
    progress.total = 1200
    progress.upload_done = True
    progress.download_done = True
    assert "حدودی" not in progress.text()
    assert "آپلود: 100٪" in progress.text()


def test_external_fetch_and_cached_delivery_do_not_show_fake_download_percent():
    for phase in ("external", "reused"):
        assert "100٪" not in TransferProgress(phase=phase).text()
    assert "در اختیار ربات نیست" in TransferProgress(phase="external").text()


async def test_reporter_does_not_repeat_unchanged_text_and_stops_after_exit():
    edited = asyncio.Event()
    messages = []
    progress = TransferProgress()

    async def edit(text, **kwargs):
        messages.append(text)
        edited.set()

    reporter = ProgressReporter(SimpleNamespace(edit=edit), progress, interval=0.01)
    async with reporter:
        progress.phase = "external"
        await asyncio.wait_for(edited.wait(), 1)
        progress.phase = "done"
    assert messages == [TransferProgress(phase="external").text(), "ارسال کامل شد ✅"]
    assert reporter._task.done()


async def test_edit_flood_wait_does_not_delay_or_fail_the_transfer():
    calls = []
    progress = TransferProgress()

    async def edit(text, **kwargs):
        calls.append(text)
        raise errors.FloodWaitError(request=None, capture=600)

    reporter = ProgressReporter(SimpleNamespace(edit=edit), progress)
    progress.phase = "external"
    await asyncio.wait_for(reporter._edit(), 1)
    progress.phase = "done"
    await reporter._edit()
    assert len(calls) == 1


async def test_terminal_status_retries_a_temporary_edit_failure():
    calls = []
    progress = TransferProgress()

    async def edit(text, **options):
        calls.append((text, options))
        if len(calls) == 1:
            raise OSError("connection reset")

    async with ProgressReporter(SimpleNamespace(edit=edit), progress, clear_buttons_on_exit=True):
        progress.phase = "done"
    assert len(calls) == 2
    assert all(text == progress.text() for text, _ in calls)
    assert calls[-1][1]["buttons"] is None


async def test_terminal_status_waits_for_the_existing_short_edit_cooldown():
    calls = []
    progress = TransferProgress()

    async def edit(text, **options):
        calls.append(text)
        if len(calls) == 1:
            error = errors.FloodWaitError(request=None, capture=1)
            error.seconds = 0.02
            raise error

    async with ProgressReporter(SimpleNamespace(edit=edit), progress) as reporter:
        progress.phase = "external"
        await reporter._edit()
        progress.phase = "done"
    assert calls == [TransferProgress(phase="external").text(), progress.text()]


async def test_exit_finishes_inflight_edit_before_publishing_terminal_status():
    editing, release = asyncio.Event(), asyncio.Event()
    applied, cancelled = [], []
    progress = TransferProgress()

    async def edit(text, **options):
        if not applied:
            editing.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(text)
                raise
        applied.append(text)

    reporter = ProgressReporter(SimpleNamespace(edit=edit), progress, interval=0.001)
    await reporter.__aenter__()
    progress.phase = "external"
    await asyncio.wait_for(editing.wait(), 1)
    progress.phase = "done"
    closing = asyncio.create_task(reporter.__aexit__(None, None, None))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(closing, 1)
    assert not cancelled
    assert applied == [TransferProgress(phase="external").text(), progress.text()]
    assert reporter._task.done()


@pytest.mark.parametrize("phase", ["done", "error", "cancelled"])
async def test_long_edit_cooldown_removes_stale_status_without_retrying_early(phase, caplog):
    edits, deleted = [], []
    progress = TransferProgress()

    async def edit(text, **options):
        edits.append(text)
        raise errors.FloodWaitError(request=None, capture=600)

    async def delete():
        deleted.append(True)

    async with ProgressReporter(SimpleNamespace(edit=edit, delete=delete), progress) as reporter:
        progress.phase = "streaming"
        await reporter._edit()
        progress.phase = phase
    assert len(edits) == 1 and deleted == [True]
    assert any(record.msg["event"] == "transfer_status_unconfirmed" for record in caplog.records)


async def test_permanent_edit_failure_does_not_retry_or_fail_the_file():
    calls = []

    async def edit(text, **options):
        calls.append("edit")
        raise errors.MessageIdInvalidError(request=None)

    async def delete():
        calls.append("delete")

    progress = TransferProgress()
    async with ProgressReporter(SimpleNamespace(edit=edit, delete=delete), progress):
        progress.phase = "done"
    assert calls == ["edit", "delete"]


async def test_unreachable_telegram_has_bounded_retries_and_safe_cleanup_logs(monkeypatch, caplog):
    calls = []
    secret = "signed-url-and-session-must-not-be-logged"

    async def edit(text, **options):
        calls.append("edit")
        raise OSError(secret)

    async def delete():
        calls.append("delete")
        raise OSError(secret)

    monkeypatch.setattr(ProgressReporter, "RETRY_DELAY", 0)
    progress = TransferProgress()
    async with ProgressReporter(SimpleNamespace(edit=edit, delete=delete), progress):
        progress.phase = "done"
    assert calls == ["edit", "edit", "edit", "delete"]
    assert secret not in caplog.text
    assert any(record.msg["event"] == "transfer_status_cleanup_failed" for record in caplog.records)


async def test_terminal_edit_timeout_retries_and_leaves_no_running_task(monkeypatch):
    calls = []

    async def edit(text, **options):
        calls.append(text)
        if len(calls) == 1:
            await asyncio.Event().wait()

    monkeypatch.setattr(ProgressReporter, "EDIT_TIMEOUT", 0.01)
    monkeypatch.setattr(ProgressReporter, "RETRY_DELAY", 0)
    progress = TransferProgress()
    async with ProgressReporter(SimpleNamespace(edit=edit), progress) as reporter:
        progress.phase = "done"
    assert calls == [progress.text(), progress.text()]
    assert reporter._task.done()


async def test_already_applied_terminal_text_is_accepted_and_clears_stop_buttons():
    async def edit(text, **options):
        assert options["buttons"] is None
        raise errors.MessageNotModifiedError(request=None)

    progress = TransferProgress()
    async with ProgressReporter(
        SimpleNamespace(edit=edit), progress, clear_buttons_on_exit=True
    ) as reporter:
        progress.phase = "done"
    assert reporter._last == progress.text() and reporter._buttons_cleared


async def test_thousand_terminal_reporters_finalize_independently():
    started, release = asyncio.Event(), asyncio.Event()
    active = 0

    async def edit(text, **options):
        nonlocal active
        active += 1
        if active == 1000:
            started.set()
        await release.wait()

    async def finish():
        progress = TransferProgress()
        async with ProgressReporter(SimpleNamespace(edit=edit), progress) as reporter:
            progress.phase = "done"
        assert reporter._task.done()

    tasks = [asyncio.create_task(finish()) for _ in range(1000)]
    try:
        await asyncio.wait_for(started.wait(), 2)
    finally:
        release.set()
        await asyncio.gather(*tasks)
