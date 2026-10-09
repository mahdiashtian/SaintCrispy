import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from downloader_bot.models import DownloadError
from downloader_bot.progress import TransferProgress
from downloader_bot.telemetry import error_fields


@dataclass
class TransferJob:
    token: str
    owner_id: int
    chat_id: int
    progress: TransferProgress
    cancelled: bool = False
    task: asyncio.Task | None = field(default=None, repr=False)


class TransferJobs:
    """Start every accepted transfer immediately; keep only a bounded task registry."""

    def __init__(self, capacity: int = 1000, telemetry=None):
        if capacity < 1:
            raise ValueError("Request capacity must be positive")
        self.capacity = capacity
        self.telemetry = telemetry
        self._jobs: dict[str, TransferJob] = {}
        self._idle = asyncio.Event()
        self._idle.set()
        self._started = False
        self._closing = False

    @property
    def count(self) -> int:
        return len(self._jobs)

    @property
    def active_count(self) -> int:
        return sum(job.task is not None for job in self._jobs.values())

    def reserve(self, owner_id: int, chat_id: int, progress: TransferProgress) -> TransferJob:
        if self._closing or self.count >= self.capacity:
            if self.telemetry is not None:
                self.telemetry.counters["capacity_rejections"] += 1
                self.telemetry.emit("request_rejected", reason="capacity", capacity=self.capacity)
            raise DownloadError("تعداد درخواست‌های همزمان به سقف رسیده؛ کمی بعد امتحان کن.")
        job = TransferJob(secrets.token_hex(8), owner_id, chat_id, progress)
        self._jobs[job.token] = job
        self._idle.clear()
        if self.telemetry is not None:
            self.telemetry.counters["jobs_reserved"] += 1
        return job

    async def join(self) -> None:
        await self._idle.wait()

    def _remove(self, token: str) -> None:
        job = self._jobs.pop(token, None)
        if job is not None and self.telemetry is not None:
            self.telemetry.counters["jobs_finished"] += 1
            if job.task is not None:
                self.telemetry.counters["jobs_ended_running"] += 1
        if not self._jobs:
            self._idle.set()

    def start(self, job: TransferJob, work: Callable[[], Awaitable[None]]) -> bool:
        if job.cancelled or self._closing:
            return False
        if self._jobs.get(job.token) is not job or job.task is not None:
            raise ValueError("Only a reserved job can be started once")
        job.task = asyncio.create_task(self._run(job, work))
        if self.telemetry is not None:
            self.telemetry.counters["jobs_started"] += 1
        # Cancellation before the first step does not execute the coroutine's finally.
        job.task.add_done_callback(lambda _: self._remove(job.token))
        return True

    async def _run(self, job: TransferJob, work) -> None:
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.telemetry is not None:
                self.telemetry.emit(
                    "job_failure", transfer_id=job.progress.transfer_id, **error_fields(error)
                )
            job.progress.error = "انتقال به علت خطای داخلی کامل نشد؛ دوباره امتحان کن."
            job.progress.phase = "error"

    def get(self, token: str, owner_id: int, chat_id: int) -> TransferJob:
        job = self._jobs.get(token)
        if job is None:
            raise DownloadError("این درخواست پایان یافته یا دیگر فعال نیست.")
        if (job.owner_id, job.chat_id) != (owner_id, chat_id):
            raise DownloadError("این دکمه مربوط به درخواست تو نیست.")
        return job

    def cancel(self, token: str, owner_id: int, chat_id: int) -> bool:
        job = self.get(token, owner_id, chat_id)
        if job.progress.phase in {"publishing", "saving", "done"}:
            if self.telemetry is not None:
                self.telemetry.emit(
                    "cancellation_rejected",
                    transfer_id=job.progress.transfer_id,
                    phase=job.progress.phase,
                )
            return False
        if not job.cancelled and self.telemetry is not None:
            self.telemetry.counters["cancellation_requests"] += 1
            self.telemetry.emit("cancellation_requested", transfer_id=job.progress.transfer_id)
        job.cancelled = True
        job.progress.phase = "cancelled"
        if job.task is None:
            self._remove(token)
        elif not job.task.cancelling():
            job.task.cancel()
        return True

    async def __aenter__(self):
        if self._started or self._closing:
            raise RuntimeError("TransferJobs can only be started once")
        self._started = True
        return self

    async def __aexit__(self, *args):
        self._closing = True
        tasks = []
        for job in tuple(self._jobs.values()):
            if job.task is None:
                self.cancel(job.token, job.owner_id, job.chat_id)
                continue
            tasks.append(job.task)
            if job.progress.phase not in {"publishing", "saving", "done"}:
                self.cancel(job.token, job.owner_id, job.chat_id)
        # Do not cancel a second time while HTTP/process cleanup is awaiting.
        await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
        self._idle.set()
