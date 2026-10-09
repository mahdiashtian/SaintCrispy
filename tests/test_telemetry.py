import asyncio
import gc
import json
import queue
import weakref
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from telethon import errors, functions, types
from video_fixture import mp4_header

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.jobs.transfers import TransferJobs
from downloader_bot.bot.progress import TransferProgress
from downloader_bot.bot.transfers.streaming import PART_SIZE
from downloader_bot.core.log_writer import JsonLogWriter
from downloader_bot.schemas.media import DownloadError, Media, Quality, Source, TelegramFile
from downloader_bot.services.download import DownloadService
from downloader_bot.services.limits import RequestLimiter
from downloader_bot.services.observability import (
    SystemSampler,
    Telemetry,
    current_transfer,
    error_fields,
)

QUALITY = Quality("original", "Original", "mp4", None, "mp4", "video/mp4", "progressive", "")
MEDIA = Media("youtube", "stable-id", "PRIVATE TITLE", "", 3, "PRIVATE URL", None, (QUALITY,))
PEER = types.InputPeerUser(1, 2)
FILE = TelegramFile(1, 2, b"private-reference", bytes(PEER), 3, 123, True)


def test_provider_failure_codes_are_logged_without_the_exception_text():
    error = DownloadError(
        "PRIVATE TEXT https://cdn.example?token=SECRET", code="origin_unavailable"
    )
    record = error_fields(error)
    assert record["error_code"] == "origin_unavailable"
    assert "PRIVATE" not in json.dumps(record) and "SECRET" not in json.dumps(record)


async def test_shared_failure_cooldown_keeps_the_provider_reason_code():
    calls = []
    writer = MemoryWriter()
    telemetry = Telemetry(writer)

    async def inspect(url):
        calls.append(url)
        raise DownloadError("PRIVATE RESPONSE", code="soundcloud_protected_stream")

    service = DownloadService(SimpleNamespace(inspect=inspect), None, None, telemetry=telemetry)
    for _ in range(2):
        with pytest.raises(DownloadError) as failure:
            await service.inspect("https://example?token=SECRET")
        assert failure.value.code == "soundcloud_protected_stream"
    assert len(calls) == 1
    records = [record for record in writer.records if record["event"] == "inspection_finished"]
    assert all(record["error_code"] == "soundcloud_protected_stream" for record in records)
    assert "PRIVATE RESPONSE" not in json.dumps(records) and "SECRET" not in json.dumps(records)


class MemoryWriter:
    def __init__(self):
        self.records = []
        self.queue = queue.Queue()
        self.dropped = self.write_errors = 0

    def emit(self, record):
        self.records.append(record)


class Repository:
    def __init__(self):
        self.files = {}

    def key(self, *parts):
        return ":".join(parts)

    async def get(self, *parts):
        return self.files.get(parts)

    async def save(self, *args):
        self.files[args[:3]] = args[3]


class Telegram:
    def __init__(self, size, *, external=False, flood=False):
        self.size = size
        self.external = external
        self.flood = flood
        self.calls = 0

    async def __call__(self, request):
        if isinstance(request, functions.messages.UploadMediaRequest):
            if not self.external:
                raise errors.WebpageCurlFailedError(request=None)
            return SimpleNamespace(
                document=SimpleNamespace(
                    id=1,
                    access_hash=2,
                    file_reference=b"ref",
                    size=self.size,
                    attributes=[types.DocumentAttributeVideo(3, 32, 32, supports_streaming=True)],
                )
            )
        self.calls += 1
        if self.flood and self.calls == 1:
            raise errors.FloodWaitError(request=None, capture=1)
        await asyncio.sleep(0)
        return True

    async def send_file(self, *args, **kwargs):
        return SimpleNamespace(
            id=3,
            document=SimpleNamespace(id=1, access_hash=2, file_reference=b"ref", size=self.size),
        )


def finished(writer):
    return [record for record in writer.records if record["event"] == "transfer_finished"]


async def make_service(http, telegram, telemetry, repository=None):
    async def resolve(*args):
        return Source(
            "https://cdn.example/media?token=SECRET", "progressive", size_bytes=telegram.size
        )

    return DownloadService(
        SimpleNamespace(resolve=resolve),
        repository or Repository(),
        TelegramDelivery(telegram, http, "", upload_parallelism=4),
        telemetry=telemetry,
    )


async def test_streamed_transfer_logs_exact_bytes_stages_and_batch_without_secrets():
    data = mp4_header() + b"x" * (PART_SIZE + 17)
    writer = MemoryWriter()
    telemetry = Telemetry(writer)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=data))
    ) as http:
        service = await make_service(http, Telegram(len(data)), telemetry)
        assert await service.deliver(PEER, MEDIA, QUALITY) == "transferred"
    record = finished(writer)[0]
    assert record["file_size_bytes"] == len(data)
    assert record["method"] == "progressive" and record["download_measurement"] == "http_body"
    assert (
        record["bytes"]["stream_read_bytes"]
        == record["bytes"]["progressive_download_bytes"]
        == len(data)
    )
    assert (
        record["bytes"]["upload_acked_bytes"]
        == record["bytes"]["upload_attempt_bytes"]
        == len(data)
    )
    assert record["counters"]["external_fallbacks"] == 1
    assert record["download_seconds"] > 0 and record["upload_seconds"] > 0
    assert {"producer_wait", "resolve_wait", "resolve", "publish", "database_save"} <= record[
        "stages_seconds"
    ].keys()
    batch = next(record for record in writer.records if record["event"] == "batch_finished")
    assert batch["totals"]["started"] == batch["totals"]["success"] == 1
    assert batch["totals"]["stream_read_bytes"] == len(data)
    assert batch["wall_seconds"] >= record["elapsed_seconds"]
    serialized = json.dumps(writer.records)
    for secret in ("SECRET", "PRIVATE TITLE", "PRIVATE URL", "https://", "private-reference"):
        assert secret not in serialized
    assert current_transfer.get() is None and not telemetry.active and telemetry.batch is None


@pytest.mark.parametrize("cached", [False, True])
async def test_external_and_cached_files_do_not_invent_local_download_upload_times(cached):
    writer = MemoryWriter()
    telemetry = Telemetry(writer)
    repository = Repository()
    if cached:
        await repository.save(MEDIA.site, MEDIA.content_id, QUALITY.key, FILE)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No origin body should be fetched"))
    ) as http:
        service = await make_service(
            http, Telegram(FILE.size_bytes, external=True), telemetry, repository
        )
        await service.deliver(PEER, MEDIA, QUALITY)
    record = finished(writer)[0]
    assert record["method"] == ("reused" if cached else "external")
    assert record["download_seconds"] is record["upload_seconds"] is None
    assert record["download_measurement"] is None
    assert (
        record["bytes"].get("stream_read_bytes", 0)
        == record["bytes"].get("upload_acked_bytes", 0)
        == 0
    )
    assert record["file_size_bytes"] == FILE.size_bytes
    assert ("cached_send" if cached else "external_fetch") in record["stages_seconds"]


async def test_explicit_flood_wait_records_retried_payload_separately_from_acknowledged_bytes():
    data = mp4_header() + b"x" * 123
    writer = MemoryWriter()
    telemetry = Telemetry(writer)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=data))
    ) as http:
        service = await make_service(http, Telegram(len(data), flood=True), telemetry)
        await service.deliver(PEER, MEDIA, QUALITY)
    record = finished(writer)[0]
    assert record["bytes"]["upload_attempt_bytes"] == len(data) * 2
    assert record["bytes"]["upload_acked_bytes"] == len(data)
    assert record["counters"]["flood_wait_events"] == 1
    assert record["stages_seconds"]["telegram_wait"] >= 1


@pytest.mark.parametrize("cancelled", [False, True])
async def test_errors_and_cancellation_always_finish_trace_and_release_context(cancelled):
    writer = MemoryWriter()
    telemetry = Telemetry(writer)
    entered = asyncio.Event()

    async def resolve(*args):
        entered.set()
        if cancelled:
            await asyncio.Future()
        raise httpx.HTTPStatusError(
            "SECRET https://signed.example?token=PRIVATE",
            request=httpx.Request("GET", "https://signed.example"),
            response=httpx.Response(403),
        )

    service = DownloadService(
        SimpleNamespace(resolve=resolve), Repository(), None, telemetry=telemetry
    )
    task = asyncio.create_task(service.deliver(PEER, MEDIA, QUALITY))
    await entered.wait()
    if cancelled:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancelled else httpx.HTTPStatusError):
        await task
    record = finished(writer)[0]
    assert record["outcome"] == ("cancelled" if cancelled else "failed")
    assert record["failed_stage"] == "resolve"
    if not cancelled:
        assert record["http_status"] == 403
    assert "SECRET" not in json.dumps(writer.records) and "signed.example" not in json.dumps(
        writer.records
    )
    assert not telemetry.active and current_transfer.get() is None


async def test_total_timeout_is_logged_as_failure_not_user_cancellation():
    writer = MemoryWriter()
    telemetry = Telemetry(writer)

    async def resolve(*args):
        await asyncio.Future()

    service = DownloadService(
        SimpleNamespace(resolve=resolve),
        Repository(),
        None,
        telemetry=telemetry,
        transfer_timeout=0.01,
    )
    with pytest.raises(TimeoutError):
        await service.deliver(PEER, MEDIA, QUALITY)
    assert finished(writer)[0]["outcome"] == "failed"
    assert finished(writer)[0]["error_type"] == "TimeoutError"
    assert not telemetry.active


def test_overlapping_download_and_upload_are_wall_spans_not_added_durations(monkeypatch):
    clock = [0.0]
    telemetry = Telemetry(MemoryWriter())
    monkeypatch.setattr("downloader_bot.services.observability.time.monotonic", lambda: clock[0])
    with telemetry.transfer(MEDIA, QUALITY) as trace:
        trace.start_phase("download")
        clock[0] = 2
        trace.start_phase("upload")
        clock[0] = 4
        trace.end_phase("download")
        clock[0] = 8
        trace.end_phase("upload")
    record = finished(telemetry.writer)[0]
    assert record["download_seconds"] == 4 and record["upload_seconds"] == 6
    assert record["elapsed_seconds"] == 8


async def test_thousand_transfers_remain_live_when_the_log_queue_is_full():
    writer = JsonLogWriter(None, capacity=5, stdout=False)
    telemetry = Telemetry(writer)
    release = asyncio.Event()

    async def new_file(*args):
        await release.wait()
        return FILE

    async def resolve(*args):
        return Source("url", "progressive")

    service = DownloadService(
        SimpleNamespace(resolve=resolve),
        Repository(),
        SimpleNamespace(new_file=new_file),
        telemetry=telemetry,
        concurrency=1000,
    )
    tasks = [
        asyncio.create_task(service.deliver(PEER, replace(MEDIA, content_id=str(index)), QUALITY))
        for index in range(1000)
    ]
    await asyncio.sleep(0)
    assert len(telemetry.active) == 1000
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert telemetry.counters["success"] == 1000 and telemetry.peak == 1000
    assert writer.queue.qsize() == 5 and writer.dropped > 1000
    assert not telemetry.active and telemetry.batch is None


async def test_periodic_snapshot_reports_active_progress_partial_traffic_and_counts(monkeypatch):
    writer = MemoryWriter()
    telemetry = Telemetry(writer)
    monkeypatch.setattr(
        telemetry.sampler,
        "sample",
        lambda: {"network_received_bytes": 11, "network_sent_bytes": 13},
    )
    with telemetry.transfer(MEDIA, QUALITY) as trace:
        trace.add("stream_read_bytes", 17)
        trace.start_phase("download")
        await telemetry.snapshot(loop_delay=0.3)
    progress = next(record for record in writer.records if record["event"] == "transfer_progress")
    assert progress["bytes"]["stream_read_bytes"] == 17
    assert progress["running_phases_seconds"]["download"] >= 0
    interval = next(record for record in writer.records if record["event"] == "metrics_interval")
    assert interval["active_transfers"] == 1 and interval["event_loop_delay_seconds"] == 0.3
    assert interval["interval"]["stream_read_bytes"] == 17
    assert interval["totals"]["network_received_bytes"] == 11
    await telemetry.snapshot()
    interval = writer.records[-1]
    assert interval["interval"]["stream_read_bytes"] == 0
    assert interval["totals"]["network_received_bytes"] == 22


async def test_inspection_logs_provider_then_memory_cache_without_url():
    writer = MemoryWriter()
    telemetry = Telemetry(writer)

    async def inspect(url):
        return MEDIA

    service = DownloadService(SimpleNamespace(inspect=inspect), None, None, telemetry=telemetry)
    for _ in range(2):
        await service.inspect("https://example?token=SECRET")
    records = [record for record in writer.records if record["event"] == "inspection_finished"]
    assert [record["path"] for record in records] == ["provider", "memory_cache"]
    assert "SECRET" not in json.dumps(records) and "https://" not in json.dumps(records)


def test_network_sampler_selects_interface_excludes_loopback_and_handles_reset(monkeypatch):
    def counters(received, sent):
        return SimpleNamespace(bytes_recv=received, bytes_sent=sent)

    samples = iter(
        [
            {"eth0": counters(100, 200), "lo": counters(900, 900)},
            {"eth0": counters(150, 230), "lo": counters(1900, 1900)},
            {"eth0": counters(5, 7)},
        ]
    )
    monkeypatch.setattr(
        "downloader_bot.services.observability.psutil.net_io_counters", lambda **_: next(samples)
    )
    sampler = SystemSampler("eth0")
    assert sampler.sample()["network_received_bytes"] == 0
    sample = sampler.sample()
    assert sample["network_interfaces"] == ["eth0"]
    assert sample["network_received_bytes"] == 50 and sample["network_sent_bytes"] == 30
    assert sampler.sample()["network_received_bytes"] == 0


async def test_writer_flushes_valid_json_and_rotates_bounded_files(tmp_path):
    path = tmp_path / "performance.jsonl"
    writer = JsonLogWriter(str(path), max_bytes=5000, backups=2, stdout=False)
    async with Telemetry(writer, interval=3600) as telemetry:
        for index in range(200):
            telemetry.emit("sample", index=index)
    paths = list(tmp_path.glob("performance.jsonl*"))
    assert len(paths) == 3
    assert all(file.stat().st_size < 5000 for file in paths)
    records = [json.loads(line) for file in paths for line in file.read_text().splitlines()]
    assert any(record["event"] == "runtime_stopped" for record in records)
    assert writer.dropped == writer.write_errors == 0 and not writer._thread.is_alive()


async def test_disk_failure_does_not_abort_transfers_or_shutdown(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied")
    writer = JsonLogWriter(str(blocked / "performance.jsonl"), stdout=False)
    async with Telemetry(writer, interval=3600) as telemetry:
        for _ in range(3):
            with telemetry.transfer(MEDIA, QUALITY):
                pass
    assert telemetry.counters["success"] == 3
    assert writer.write_errors >= 1 and writer.dropped > 0
    assert not writer._thread.is_alive()


async def test_admission_cancellation_and_job_errors_have_safe_correlated_events():
    writer = MemoryWriter()
    telemetry = Telemetry(writer)
    limits = RequestLimiter(60, telemetry=telemetry)
    await limits.check(987654321, "inspect")
    with pytest.raises(DownloadError):
        await limits.check(987654321, "inspect")
    async with TransferJobs(1, telemetry=telemetry) as jobs:
        job = jobs.reserve(987654321, 987654322, TransferProgress())
        with pytest.raises(DownloadError):
            jobs.reserve(987654321, 987654322, TransferProgress())
        assert jobs.cancel(job.token, job.owner_id, job.chat_id)
        job = jobs.reserve(987654321, 987654322, TransferProgress())

        async def broken():
            raise RuntimeError("PRIVATE SECRET")

        jobs.start(job, broken)
        await jobs.join()
    assert telemetry.counters["rate_rejections"] == telemetry.counters["capacity_rejections"] == 1
    assert telemetry.counters["jobs_reserved"] == telemetry.counters["jobs_finished"] == 2
    error = next(record for record in writer.records if record["event"] == "job_failure")
    assert error["transfer_id"] == job.progress.transfer_id
    text = json.dumps(writer.records)
    assert all(value not in text for value in ("987654321", "987654322", "PRIVATE SECRET"))


async def test_interrupted_source_logs_partial_bytes_without_fictitious_success():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            header = mp4_header()
            yield header + b"x" * (PART_SIZE - len(header))
            await asyncio.sleep(0.01)
            raise OSError("PRIVATE SECRET")

    writer = MemoryWriter()
    telemetry = Telemetry(writer)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=BrokenStream()))
    ) as http:
        service = await make_service(http, Telegram(2 * PART_SIZE), telemetry)
        with pytest.raises(OSError):
            await service.deliver(PEER, MEDIA, QUALITY)
    record = finished(writer)[0]
    assert record["outcome"] == "failed"
    assert record["failed_stage"] == "source_stream"
    assert record["bytes"]["stream_read_bytes"] == PART_SIZE
    assert record["bytes"]["upload_acked_bytes"] == PART_SIZE
    assert record["counters"].get("telegram_publications", 0) == 0
    assert "PRIVATE SECRET" not in json.dumps(writer.records)


def test_caught_retry_errors_do_not_retain_payloads_through_their_tracebacks():
    class Payload:
        pass

    references = []

    def rejected_rpc():
        payload = Payload()
        references.append(weakref.ref(payload))
        raise OSError("retry")

    telemetry = Telemetry(MemoryWriter())
    with telemetry.transfer(MEDIA, QUALITY) as trace:
        with pytest.raises(OSError):
            with trace.span("upload_rpc"):
                rejected_rpc()
        gc.collect()
        assert references[0]() is None
