import math
import time
from collections import OrderedDict

from downloader_bot.models import DownloadError


def check_file_size(size: int | None, maximum: int) -> None:
    if maximum and size is not None and size > maximum:
        raise DownloadError(f"حجم فایل بیشتر از سقف مجاز {maximum // (1024 * 1024)} مگابایت است.")


class RequestLimiter:
    """One interval per user and stage, across all chats; production uses PostgreSQL."""

    def __init__(self, interval_seconds: int = 60, repository=None):
        if interval_seconds < 1:
            raise ValueError("Request interval must be positive")
        self.interval = interval_seconds
        self.repository = repository
        self._deadlines: OrderedDict[tuple[int, str], float] = OrderedDict()

    async def check(self, user_id: int, scope: str) -> None:
        if self.repository is not None:
            remaining = await self.repository.claim_request(user_id, scope, self.interval)
        else:
            now = time.monotonic()
            while self._deadlines and next(iter(self._deadlines.values())) <= now:
                self._deadlines.popitem(last=False)
            key = user_id, scope
            remaining = math.ceil(self._deadlines.get(key, now) - now)
            if remaining <= 0:
                self._deadlines[key] = now + self.interval
        if remaining > 0:
            raise DownloadError(
                f"هر {self.interval} ثانیه یک درخواست مجاز است؛ {remaining} ثانیه دیگر امتحان کن."
            )
