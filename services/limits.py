import math
import time
from collections import OrderedDict

from core.locks import KeyedLocks
from schemas.media import DownloadError
from services.observability import error_fields


def check_file_size(size: int | None, maximum: int) -> None:
    if maximum and size is not None and size > maximum:
        raise DownloadError(f"حجم فایل بیشتر از سقف مجاز {maximum // (1024 * 1024)} مگابایت است.")


class RequestLimiter:
    """One interval per user and stage, across all chats; production uses PostgreSQL."""

    def __init__(
        self,
        interval_seconds: int = 60,
        repository=None,
        telemetry=None,
        *,
        cache_capacity=100000,
        admission_repository=None,
    ):
        if interval_seconds < 1:
            raise ValueError("Request interval must be positive")
        self.interval = interval_seconds
        self.repository = repository
        self.admission_repository = admission_repository
        self.telemetry = telemetry
        if cache_capacity < 1:
            raise ValueError("Admission cache capacity must be positive")
        self.cache_capacity = cache_capacity
        self._locks = KeyedLocks()
        self._deadlines: OrderedDict[tuple[int, str], float] = OrderedDict()

    async def check_link(self, user_id, chat_id, request_id) -> bool:
        if self.admission_repository is None:
            await self.check(user_id, "inspect")
            return False
        await self.check(
            user_id,
            "inspect",
            claim=lambda: self.admission_repository.claim_link(
                user_id, chat_id, request_id, self.interval
            ),
        )
        return True

    async def check(self, user_id: int, scope: str, *, claim=None) -> None:
        key = user_id, scope
        async with self._locks.hold(f"{user_id}:{scope}"):
            now = time.monotonic()
            while self._deadlines and next(iter(self._deadlines.values())) <= now:
                self._deadlines.popitem(last=False)
            remaining = math.ceil(self._deadlines.get(key, now) - now)
            if remaining <= 0:
                if self.repository is not None or claim is not None:
                    try:
                        if self.telemetry is not None:
                            self.telemetry.counters["admission_database_checks"] += 1
                        remaining = (
                            await claim()
                            if claim is not None
                            else await self.repository.claim_request(user_id, scope, self.interval)
                        )
                    except Exception as error:
                        if self.telemetry is not None:
                            self.telemetry.emit(
                                "request_admission_failure", scope=scope, **error_fields(error)
                            )
                        raise
                else:
                    remaining = 0
                # Local leases can only deny: every new grant still comes from SQL.
                deadline = now + self.interval if remaining == 0 else time.monotonic() + remaining
                self._deadlines[key] = deadline
                self._deadlines.move_to_end(key)
                if len(self._deadlines) > self.cache_capacity:
                    self._deadlines.popitem(last=False)
            elif self.telemetry is not None:
                self.telemetry.counters["admission_local_rejections"] += 1
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
                f"هر {self.interval} ثانیه یک درخواست مجاز است؛ {remaining} ثانیه دیگر امتحان کن.",
                code="user_rate_limited",
            )
