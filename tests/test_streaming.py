import asyncio

import pytest

from downloader_bot.bot.transfers.streaming import PART_SIZE, upload_stream
from downloader_bot.schemas.media import DownloadError


@pytest.mark.parametrize("size", [1, 1024, PART_SIZE - 1, PART_SIZE, PART_SIZE + 7, 2 * PART_SIZE])
async def test_unknown_length_upload_finalizes_correctly(size):
    sent = []

    async def client(request):
        sent.append(request)
        return True

    async def chunks():
        remaining = size
        while remaining:
            count = min(37111, remaining)
            yield b"a" * count
            remaining -= count

    handle = await upload_stream(client, chunks(), "audio.m4a")
    assert sum(len(part.bytes) for part in sent) == size
    assert all(part.file_total_parts == -1 for part in sent[:-1])
    assert sent[-1].file_total_parts == (size + PART_SIZE - 1) // PART_SIZE
    assert handle.parts == sent[-1].file_total_parts
    assert [part.file_part for part in sent] == list(range(len(sent)))
    assert len(sent[-1].bytes) == size % PART_SIZE


async def test_failed_input_never_produces_a_completed_file():
    sent = []

    async def client(request):
        sent.append(request)
        return True

    async def chunks():
        yield b"a" * PART_SIZE
        raise RuntimeError("interrupted source")

    with pytest.raises(RuntimeError):
        await upload_stream(client, chunks(), "broken.m4a")
    assert len(sent) == 1
    assert sent[0].file_total_parts == -1


async def test_cancellation_closes_the_source():
    closed = asyncio.Event()
    part_started = asyncio.Event()

    async def chunks():
        try:
            yield b"a" * PART_SIZE
        finally:
            closed.set()

    async def client(request):
        part_started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(upload_stream(client, chunks(), "cancelled.m4a"))
    await part_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_empty_source_is_rejected():
    async def chunks():
        if False:
            yield b""

    async def client(request):
        pytest.fail("An empty stream must not be uploaded")

    with pytest.raises(DownloadError):
        await upload_stream(client, chunks(), "empty.m4a")


async def test_parallel_upload_has_backpressure_and_finalizes_after_acknowledgements():
    gate, full = asyncio.Event(), asyncio.Event()
    active = peak = produced = 0
    sent, acknowledged = [], []

    async def chunks():
        nonlocal produced
        for _ in range(12):
            produced += 1
            yield b"a" * PART_SIZE
        yield b"tail"

    async def client(request):
        nonlocal active, peak
        if request.file_total_parts != -1:
            assert len(acknowledged) == 12
        active += 1
        peak = max(peak, active)
        sent.append(request)
        if active == 4:
            full.set()
        try:
            await gate.wait()
            acknowledged.append(request.file_part)
            return True
        finally:
            active -= 1

    task = asyncio.create_task(upload_stream(client, chunks(), "test.mp4", parallelism=4))
    await asyncio.wait_for(full.wait(), 1)
    assert peak == 4 and produced <= 5  # One producer chunk beyond the bounded window.
    gate.set()
    handle = await asyncio.wait_for(task, 2)
    assert handle.parts == 13 and peak == 4
    assert sorted(acknowledged) == list(range(13))
    assert sent[-1].bytes == b"tail" and sent[-1].file_total_parts == 13


async def test_cancelling_parallel_upload_closes_source_and_all_part_requests():
    ready, source_closed = asyncio.Event(), asyncio.Event()
    active = 0

    async def chunks():
        try:
            for _ in range(50):
                yield b"a" * PART_SIZE
        finally:
            source_closed.set()

    async def client(request):
        nonlocal active
        active += 1
        if active == 4:
            ready.set()
        try:
            await asyncio.Future()
        finally:
            active -= 1

    task = asyncio.create_task(upload_stream(client, chunks(), "test.mp4", parallelism=4))
    await asyncio.wait_for(ready.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source_closed.is_set() and active == 0


async def test_failed_parallel_part_does_not_finalize_or_leak_requests():
    active = 0
    final = False

    async def chunks():
        for _ in range(10):
            yield b"a" * PART_SIZE

    async def client(request):
        nonlocal active, final
        final |= request.file_total_parts != -1
        if request.file_part == 1:
            return False
        active += 1
        try:
            await asyncio.Future()
        finally:
            active -= 1

    with pytest.raises(DownloadError):
        await asyncio.wait_for(upload_stream(client, chunks(), "test.mp4", parallelism=4), 1)
    assert active == 0 and not final
