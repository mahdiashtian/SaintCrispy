import asyncio
from types import SimpleNamespace

import httpx
import pytest
from telethon import errors

from downloader_bot.bot.progress import ProgressReporter, TransferProgress
from downloader_bot.bot.transfers.streaming import PART_SIZE, media_chunks, upload_stream
from downloader_bot.schemas.media import DownloadError, Quality, Source

QUALITY = Quality("original", "Original", "mp3", None, "mp3", "audio/mpeg", "progressive", "url")


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
