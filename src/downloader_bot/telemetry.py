"""Transfer measurements and periodic process/network summaries without media or secrets."""

import asyncio
import hashlib
import time
import traceback
import uuid
from collections import Counter, deque
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, nullcontext, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from math import ceil
from pathlib import Path

import psutil

from downloader_bot.log_writer import JsonLogWriter

current_transfer = ContextVar("current_transfer", default=None)
current_inspection = ContextVar("current_inspection", default=None)


def error_fields(error: BaseException) -> dict:
    """Exception text, request objects, URLs and source lines can contain credentials."""
    frames = deque(traceback.walk_tb(error.__traceback__), maxlen=8)
    result = {
        "error_type": type(error).__name__,
        "error_stack": [
            {
                "file": Path(frame.f_code.co_filename).name,
                "line": line,
                "function": frame.f_code.co_name,
            }
            for frame, line in frames
        ],
    }
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None) or getattr(error, "status", None)
    if isinstance(status, int):
        result["http_status"] = status
    return result


def timed(stage: str):
    trace = current_transfer.get()
    return trace.span(stage) if trace is not None else nullcontext()


def count(name: str, value: int = 1):
    if (trace := current_transfer.get()) is not None:
        trace.add(name, value)


@asynccontextmanager
async def acquired(resource, stage: str):
    async with AsyncExitStack() as stack:
        with timed(stage):
            await stack.enter_async_context(resource)
        yield


class TransferTrace:
    def __init__(self, telemetry, media, quality, progress=None):
        self.telemetry = telemetry
        self.id = uuid.uuid4().hex
        if progress is not None:
            progress.transfer_id = self.id
        self.site = media.site
        self.quality = quality.key
        identity = f"{media.site}\n{media.content_id}\n{quality.key}"
        self.content_key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.started = time.monotonic()
        self.started_at = datetime.now(UTC).isoformat()
        self.stage_seconds = Counter()
        self.counters = Counter()
        self.method = "unknown"
        self.protocol = None
        self.file_size = None
        self.source_size = None
        self.result = None
        self.failed_stage = None
        self._stage_errors = deque(maxlen=16)
        self._phases = {}
        self.active_stages = Counter()
        self.batch_id = telemetry.batch["id"]

    @contextmanager
    def span(self, stage):
        started = time.monotonic()
        self.active_stages[stage] += 1
        try:
            yield
        except BaseException as error:
            # Tracebacks can hold entire upload payloads; retain identities only.
            if not any(previous == id(error) for previous, _ in self._stage_errors):
                self._stage_errors.append((id(error), stage))
            raise
        finally:
            self.stage_seconds[stage] += time.monotonic() - started
            self.active_stages[stage] -= 1

    def start_phase(self, phase):
        self._phases.setdefault(phase, time.monotonic())

    def discard_error(self, error):
        self._stage_errors = deque(
            ((identity, stage) for identity, stage in self._stage_errors if identity != id(error)),
            maxlen=16,
        )

    def clear_errors(self):
        self.failed_stage = None
        self._stage_errors.clear()

    def end_phase(self, phase):
        if (started := self._phases.pop(phase, None)) is not None:
            self.stage_seconds[phase] += time.monotonic() - started

    def add(self, name, value=1):
        self.counters[name] += value
        self.telemetry.counters[name] += value
        if name in {"stream_read_bytes", "upload_attempt_bytes"}:
            self.counters["pipeline_payload_bytes"] += value
            self.telemetry.counters["pipeline_payload_bytes"] += value

    def fields(self):
        return {
            "transfer_id": self.id,
            "site": self.site,
            "quality": self.quality,
            "content_key": self.content_key,
            "batch_id": self.batch_id,
        }

    def finish(self, error):
        for phase in tuple(self._phases):
            self.end_phase(phase)
        elapsed = time.monotonic() - self.started
        outcome = "success"
        if error is not None:
            outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        self.telemetry.active.pop(self.id, None)
        self.telemetry.counters[outcome] += 1
        self.telemetry.sites[self.site][outcome] += 1
        self.telemetry.elapsed_samples.append(elapsed)
        self.telemetry.counters["completed_elapsed_seconds"] += elapsed
        if outcome == "success":
            self.telemetry.counters[self.result or "transferred"] += 1
            self.telemetry.counters["delivered_file_bytes"] += self.file_size or 0
        measurement = None
        if self.protocol in {"hls", "dash"}:
            measurement = "ffmpeg_output"
        elif self.protocol == "progressive" and self.method != "external":
            measurement = "http_body"
        fields = {
            **self.fields(),
            "outcome": outcome,
            "result": self.result,
            "method": self.method,
            "source_protocol": self.protocol,
            "file_size_bytes": self.file_size,
            "source_size_bytes": self.source_size,
            "file_size_mib": round(self.file_size / 1024**2, 4)
            if self.file_size is not None
            else None,
            "elapsed_seconds": round(elapsed, 6),
            "started_at": self.started_at,
            "stages_seconds": {key: round(value, 6) for key, value in self.stage_seconds.items()},
            "download_seconds": self.stage_seconds.get("download"),
            "upload_seconds": self.stage_seconds.get("upload"),
            "download_measurement": measurement,
            "bytes": {key: value for key, value in self.counters.items() if key.endswith("_bytes")},
            "counters": {
                key: value for key, value in self.counters.items() if not key.endswith("_bytes")
            },
        }
        for phase, byte_key in (
            ("download", "stream_read_bytes"),
            ("upload", "upload_acked_bytes"),
        ):
            duration = self.stage_seconds.get(phase)
            fields[f"{phase}_mib_per_second"] = (
                round(self.counters[byte_key] / 1024**2 / duration, 4) if duration else None
            )
        if error is not None:
            cause = error
            for _ in range(8):
                stage = next(
                    (stage for previous, stage in self._stage_errors if previous == id(cause)), None
                )
                if stage is not None:
                    self.failed_stage = stage
                    break
                cause = cause.__cause__ or cause.__context__
                if cause is None:
                    break
            fields.update(error_fields(error), failed_stage=self.failed_stage)
        self._stage_errors.clear()
        self.telemetry.emit("transfer_finished", **fields)
        if not self.telemetry.active:
            self.telemetry.finish_batch()


class SystemSampler:
    """Counters belong to selected interfaces in this process's network namespace."""

    def __init__(self, interface: str = ""):
        self.interface = interface
        self.process = psutil.Process()
        self.previous = {}
        self.process.cpu_percent()
        psutil.cpu_percent()

    def sample(self):
        result = {}
        try:
            interfaces = psutil.net_io_counters(pernic=True, nowrap=True) or {}
            if self.interface:
                selected = {
                    name: value for name, value in interfaces.items() if name == self.interface
                }
            else:
                selected = {
                    name: value
                    for name, value in interfaces.items()
                    if name != "lo" and "loopback" not in name.lower()
                }
            received = sent = 0
            for name, value in selected.items():
                if name in self.previous:
                    previous = self.previous[name]
                    received += max(0, value.bytes_recv - previous.bytes_recv)
                    sent += max(0, value.bytes_sent - previous.bytes_sent)
            self.previous = selected
            result.update(
                network_interfaces=sorted(selected),
                network_available=bool(selected),
                network_received_bytes=received,
                network_sent_bytes=sent,
            )
            children = self.process.children(recursive=True)
            child_rss = 0
            for child in children:
                with suppress(psutil.Error):
                    child_rss += child.memory_info().rss
            result.update(
                process_cpu_percent=self.process.cpu_percent(),
                system_cpu_percent=psutil.cpu_percent(),
                process_rss_bytes=self.process.memory_info().rss,
                child_processes=len(children),
                child_rss_bytes=child_rss,
            )
        except (psutil.Error, OSError):
            result["sample_error"] = True
        return result


class Telemetry:
    def __init__(self, writer: JsonLogWriter, interval: float = 30, interface: str = ""):
        if interval <= 0:
            raise ValueError("Metrics interval must be positive")
        self.writer = writer
        self.interval = interval
        self.session_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.counters = Counter()
        self.sites = {}
        self.active = {}
        self.peak = 0
        self.batch = None
        self.elapsed_samples = deque(maxlen=2048)
        self.sampler = SystemSampler(interface)
        self._previous = Counter()
        self._last_snapshot = self.started
        self._task = None
        self._stop = asyncio.Event()
        self._snapshot_lock = asyncio.Lock()

    def emit(self, event: str, **fields):
        self.writer.emit(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                "session_id": self.session_id,
                **fields,
            }
        )

    @contextmanager
    def transfer(self, media, quality, progress=None):
        if not self.active:
            self.batch = {
                "id": uuid.uuid4().hex,
                "started": time.monotonic(),
                "totals": self.counters.copy(),
                "peak": 0,
            }
            self.emit("batch_started", batch_id=self.batch["id"])
        trace = TransferTrace(self, media, quality, progress)
        self.active[trace.id] = trace
        self.peak = max(self.peak, len(self.active))
        self.batch["peak"] = max(self.batch["peak"], len(self.active))
        self.counters["started"] += 1
        self.sites.setdefault(trace.site, Counter())["started"] += 1
        token = current_transfer.set(trace)
        self.emit("transfer_started", **trace.fields(), active_transfers=len(self.active))
        error = None
        try:
            yield trace
        except BaseException as failure:
            error = failure
            raise
        finally:
            trace.finish(error)
            current_transfer.reset(token)

    def finish_batch(self):
        keys = (
            "started",
            "success",
            "failed",
            "cancelled",
            "transferred",
            "reused",
            "stream_read_bytes",
            "progressive_download_bytes",
            "ffmpeg_output_bytes",
            "upload_acked_bytes",
            "upload_attempt_bytes",
            "pipeline_payload_bytes",
            "delivered_file_bytes",
        )
        self.emit(
            "batch_finished",
            batch_id=self.batch["id"],
            wall_seconds=round(time.monotonic() - self.batch["started"], 6),
            peak_active_transfers=self.batch["peak"],
            totals={key: self.counters[key] - self.batch["totals"][key] for key in keys},
        )
        self.batch = None

    @contextmanager
    def inspection(self, url):
        record = {
            "request_id": uuid.uuid4().hex,
            "url_key": hashlib.sha256(url.encode()).hexdigest()[:24],
            "path": "unknown",
        }
        token = current_inspection.set(record)
        started = time.monotonic()
        error = None
        self.counters["inspections_started"] += 1
        try:
            yield record
        except BaseException as failure:
            error = failure
            raise
        finally:
            outcome = (
                "success"
                if error is None
                else "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "failed"
            )
            self.counters[f"inspections_{outcome}"] += 1
            self.emit(
                "inspection_finished",
                **record,
                outcome=outcome,
                elapsed_seconds=round(time.monotonic() - started, 6),
                **(error_fields(error) if error else {}),
            )
            current_inspection.reset(token)

    async def snapshot(self, event="metrics_interval", loop_delay=0):
        async with self._snapshot_lock:
            await self._snapshot(event, loop_delay)

    async def _snapshot(self, event, loop_delay):
        system = await asyncio.to_thread(self.sampler.sample)
        self.counters["network_received_bytes"] += system.get("network_received_bytes", 0)
        self.counters["network_sent_bytes"] += system.get("network_sent_bytes", 0)
        self.counters["network_traffic_bytes"] = (
            self.counters["network_received_bytes"] + self.counters["network_sent_bytes"]
        )
        now = time.monotonic()
        interval = now - self._last_snapshot
        delta = {key: value - self._previous[key] for key, value in self.counters.items()}
        samples = sorted(self.elapsed_samples)
        for trace in tuple(self.active.values()):
            self.emit(
                "transfer_progress",
                **trace.fields(),
                method=trace.method,
                source_size_bytes=trace.source_size,
                elapsed_seconds=round(now - trace.started, 6),
                bytes={
                    key: value for key, value in trace.counters.items() if key.endswith("_bytes")
                },
                active_stages=[key for key, value in trace.active_stages.items() if value > 0],
                running_phases_seconds={
                    key: round(now - value, 6) for key, value in trace._phases.items()
                },
                stages_seconds={key: round(value, 6) for key, value in trace.stage_seconds.items()},
            )
        self.emit(
            event,
            uptime_seconds=round(now - self.started, 6),
            interval_seconds=round(interval, 6),
            active_transfers=len(self.active),
            reserved_jobs=self.counters["jobs_reserved"] - self.counters["jobs_finished"],
            running_jobs=self.counters["jobs_started"] - self.counters["jobs_ended_running"],
            peak_active_transfers=self.peak,
            active_methods=dict(Counter(trace.method for trace in self.active.values())),
            totals=dict(self.counters),
            interval=delta,
            rates_bytes_per_second={
                key: round(delta.get(key, 0) / interval, 3) if interval else 0
                for key in (
                    "stream_read_bytes",
                    "progressive_download_bytes",
                    "upload_acked_bytes",
                    "upload_attempt_bytes",
                    "network_received_bytes",
                    "network_sent_bytes",
                    "network_traffic_bytes",
                )
            },
            completed_per_second=round(
                sum(delta.get(key, 0) for key in ("success", "failed", "cancelled")) / interval, 4
            )
            if interval
            else 0,
            recent_elapsed_p95_seconds=round(samples[max(0, ceil(len(samples) * 0.95) - 1)], 6)
            if samples
            else None,
            elapsed_sample_count=len(samples),
            sites={key: dict(value) for key, value in self.sites.items()},
            event_loop_delay_seconds=round(loop_delay, 6),
            log_queue_pending=self.writer.queue.qsize(),
            log_records_dropped=self.writer.dropped,
            log_write_errors=self.writer.write_errors,
            system=system,
        )
        self._last_snapshot = now
        self._previous = self.counters.copy()

    async def _run(self):
        while True:
            target = time.monotonic() + self.interval
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                return
            except TimeoutError:
                await self.snapshot(loop_delay=max(0, time.monotonic() - target))

    async def __aenter__(self):
        self.writer.start()
        await asyncio.to_thread(self.sampler.sample)
        self._last_snapshot = time.monotonic()
        self.emit("runtime_started")
        self._task = asyncio.create_task(self._run(), name="performance-metrics")
        return self

    async def __aexit__(self, *args):
        if self._task is not None:
            self._stop.set()
            await self._task
        if args[1] is not None:
            event = (
                "runtime_cancelled"
                if isinstance(args[1], asyncio.CancelledError)
                else "runtime_failure"
            )
            self.emit(event, **error_fields(args[1]))
        await self.snapshot("runtime_stopped")
        await asyncio.to_thread(self.writer.close)
