import math
import time
from collections import OrderedDict

from downloader_bot.schemas.media import DownloadError
from downloader_bot.services.observability import error_fields


def check_file_size(size: int | None, maximum: int) -> None:
    if maximum and size is not None and size > maximum:
        raise DownloadError(f"حجم فایل بیشتر از سقف مجاز {maximum // (1024 * 1024)} مگابایت است.")


class RequestLimiter:
    """One interval per user and stage, across all chats; production uses PostgreSQL."""

    def __init__(self, interval_seconds: int = 60, repository=None, telemetry=None):
        if interval_seconds < 1:
            raise ValueError("Request interval must be positive")
        self.interval = interval_seconds
        self.repository = repository
        self.telemetry = telemetry
        self._deadlines: OrderedDict[tuple[int, str], float] = OrderedDict()

    async def check(self, user_id: int, scope: str) -> None:
        if self.repository is not None:
            try:
                remaining = await self.repository.claim_request(user_id, scope, self.interval)
            except Exception as error:
                if self.telemetry is not None:
                    self.telemetry.emit(
                        "request_admission_failure", scope=scope, **error_fields(error)
                    )
                raise
        else:
            now = time.monotonic()
            while self._deadlines and next(iter(self._deadlines.values())) <= now:
                self._deadlines.popitem(last=False)
            key = user_id, scope
            remaining = math.ceil(self._deadlines.get(key, now) - now)
            if remaining <= 0:
                self._deadlines[key] = now + self.interval
        if remaining > 0:
            if self.telemetry is not None:
                self.telemetry.counters["rate_rejections"] += 1
                self.telemetry.emit(
                    "request_rejected",
                    reason="rate_limit",
                    scope=scope,
                    retry_after_seconds=remaining,
                )
            raise DownloadError(
                f"هر {self.interval} ثانیه یک درخواست مجاز است؛ {remaining} ثانیه دیگر امتحان کن."
            )
