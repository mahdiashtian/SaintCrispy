"""Probe guardrails: bounded inspection, truthful results and upload cleanup."""

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from telethon import errors, functions, types

from downloader_bot.models import DownloadError
from downloader_bot.progress import TransferProgress
from downloader_bot.streaming import PART_SIZE

SPEC = importlib.util.spec_from_file_location(
    "transfer_probe_matrix",
    Path(__file__).parents[1] / "tools/probe_telegram_matrix.py",
)
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


async def test_inspection_closes_body_early_when_server_ignores_range():
    consumed, closed = 0, False

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal consumed
            for _ in range(1000):
                consumed += 1
                yield b"x" * 1024

        async def aclose(self):
            nonlocal closed
            closed = True

    def respond(request):
        assert request.headers["Range"] == "bytes=0-1023"
        return httpx.Response(200, headers={"Content-Length": "1024000"}, stream=Body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await probe.inspect(http, probe.Case("test", "https://example.com/file"))
    assert consumed == 1 and closed
    assert result["source_size"] == 1024000 and result["sample_bytes"] == 1024


async def test_external_acceptance_is_preserved_when_reference_read_fails():
    document = SimpleNamespace(
        id=7,
        access_hash=8,
        file_reference=b"private-reference",
        size=123,
        attributes=[],
        mime_type="video/mp4",
        date=datetime.now(timezone.utc),
        dc_id=2,
    )

    class Client:
        async def __call__(self, request):
            assert isinstance(request, functions.messages.UploadMediaRequest)
            assert isinstance(request.media, types.InputMediaDocumentExternal)
            return SimpleNamespace(document=document)

        def iter_download(self, *args, **kwargs):
            raise errors.FileReferenceInvalidError(request=None)

    result = await probe.external(
        Client(),
        probe.Case("signed", "https://example.com/v.mp4?token=secret"),
        types.InputPeerSelf(),
        1,
    )
    assert result["external_accepted"] and result["telegram_size"] == 123
    assert result["failed_stage"] == "reference_read"
    assert "secret" not in json.dumps(result) and "private-reference" not in json.dumps(result)


async def test_reference_read_is_limited_and_does_not_register_existing_media():
    closed = False
    document = SimpleNamespace(dc_id=2)

    class Iterator:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if getattr(self, "finished", False):
                raise StopAsyncIteration
            self.finished = True
            return b"x" * 4096

        async def close(self):
            nonlocal closed
            closed = True

    class Client:
        def iter_download(self, file, **kwargs):
            assert file is document and kwargs["limit"] == 1 and kwargs["request_size"] == 4096
            return Iterator()

        async def __call__(self, request):
            pytest.fail("Reference validation must not re-register InputMediaDocument")

    result = await probe.verify_reference(Client(), SimpleNamespace(document=document))
    assert closed and result["reference_read_ok"] and result["reference_read_bytes"] == 4096


async def test_known_size_window_is_bounded_and_preserves_all_parts():
    active = peak = 0
    parts = {}

    async def client(request):
        nonlocal active, peak
        assert request.file_total_parts == 6
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        parts[request.file_part] = request.bytes
        active -= 1
        return True

    async def chunks():
        for _ in range(5):
            yield b"x" * PART_SIZE
        yield b"end"

    progress = TransferProgress()
    handle = await probe.upload_known(client, chunks(), "test.bin", 5 * PART_SIZE + 3, progress, 3)
    assert isinstance(handle, types.InputFileBig) and handle.parts == 6
    assert peak <= 3 and progress.uploaded == 5 * PART_SIZE + 3
    assert parts[5] == b"end" and progress.upload_done


async def test_upload_cancellation_closes_source_and_cancels_all_inflight_parts():
    started, closed = asyncio.Event(), asyncio.Event()
    live = set()

    async def client(request):
        task = asyncio.current_task()
        live.add(task)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            live.remove(task)

    async def chunks():
        try:
            for _ in range(10):
                yield b"x" * PART_SIZE
        finally:
            closed.set()

    work = asyncio.create_task(
        probe.upload_known(
            client,
            chunks(),
            "test.bin",
            10 * PART_SIZE,
            TransferProgress(),
            2,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert closed.is_set() and not live


async def test_truncated_known_size_upload_never_returns_a_finished_handle():
    async def client(request):
        return True

    async def chunks():
        yield b"short"

    progress = TransferProgress()
    with pytest.raises(DownloadError, match="Source size"):
        await probe.upload_known(client, chunks(), "test.bin", 100, progress, 2)
    assert not progress.upload_done
