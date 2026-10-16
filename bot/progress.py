import asyncio
import logging
import time

from telethon import errors

from schemas.transfer import TransferState
from services.observability import error_fields

log = logging.getLogger("telegram")
TERMINAL_PHASES = {"done", "error", "cancelled"}


class TransferProgress(TransferState):
    def text(self) -> str:
        if self.phase == "done":
            if self.method in {"hls", "dash", "progressive"}:
                return (
                    "ارسال کامل شد ✅\n"
                    f"دانلود: 100٪ · {megabytes(self.downloaded)} MB\n"
                    f"آپلود: 100٪ · {megabytes(self.uploaded)} MB"
                )
            if self.method == "reused":
                return "ارسال کامل شد ✅\nفایل ذخیره‌شده رو از تلگرام دوباره فرستادم."
            if self.method == "external":
                return "ارسال کامل شد ✅\nتلگرام فایل رو مستقیم از لینک گرفت."
            return "ارسال کامل شد ✅"
        if self.phase == "error":
            return f"❌ دریافت فایل کامل نشد.\n{self.error}"
        if self.phase == "cancelled":
            return "درخواست لغو شد ⏹\nدانلود و آپلود ربات متوقف شدن."
        if self.phase in {"starting", "queued"}:
            return "📥 دارم دانلود رو شروع می‌کنم…"
        if self.phase == "resolving":
            return "🔗 دارم لینک فایل رو می‌گیرم…"
        if self.phase == "waiting_process":
            return "🎬 دارم فایل رو آماده می‌کنم…"
        if self.phase == "waiting_telegram":
            return "📤 دارم ارسال به تلگرام رو آماده می‌کنم…"
        if self.phase == "reused":
            return "♻️ دارم فایل قبلی رو از تلگرام برات می‌فرستم…"
        if self.phase == "external":
            return "⚡ تلگرام در حال دریافت مستقیم فایل است…\nدرصد این روش در اختیار ربات نیست."
        if self.phase == "saving":
            return "💾 فایل رو فرستادم؛ دارم برای دفعهٔ بعد ذخیره‌اش می‌کنم…"
        if self.phase == "publishing":
            return "📨 تلگرام داره پیام فایل رو ثبت می‌کنه…\nاین مرحله قابل لغو نیست."

        if self.download_done:
            download = "100٪"
        elif self.method in {"hls", "dash"} and self.duration:
            download = percentage(self.seconds, self.duration, False) + " (بر اساس زمان رسانه)"
        elif self.total:
            download = percentage(self.downloaded, self.total, False)
        else:
            download = "حجم کل نامشخص"

        total = self.total or self.estimated_total
        upload = percentage(self.uploaded, total, self.upload_done) if total else "حجم کل نامشخص"
        if self.total is None and self.estimated_total:
            upload += " (حدودی)"
        preparing = (
            "🎬 در حال آماده‌سازی فایل برای پخش حین دانلود…\n"
            if self.phase == "preparing_video"
            else ""
        )
        return (
            preparing + f"📥 دانلود: {download} · {megabytes(self.downloaded)} MB\n"
            f"📤 آپلود: {upload} · {megabytes(self.uploaded)} MB"
        )


def percentage(current: float, total: float, finished: bool) -> str:
    value = 100 if finished else min(99, max(0, int(current * 100 / total)))
    return f"{value}٪"


def megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.2f}"


class ProgressReporter:
    """Coalesce progress updates and confirm a bounded, ordered terminal update."""

    EDIT_TIMEOUT = 3
    FINAL_TIMEOUT = 15
    FINAL_ATTEMPTS = 3
    RETRY_DELAY = 0.5

    def __init__(
        self,
        message,
        progress: TransferProgress,
        interval: float = 2.5,
        clear_buttons_on_exit: bool = False,
        terminal_reply=None,
    ):
        self.message = message
        self.progress = progress
        self.interval = interval
        self._last = progress.text()
        self._retry_at = 0.0
        self._task = None
        self._stop = asyncio.Event()
        self._editable = True
        self._failure = {}
        self._clear_buttons_on_exit = clear_buttons_on_exit
        self._buttons_cleared = False
        self._terminal_reply = terminal_reply

    async def __aenter__(self):
        if self.message is not None:
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *args):
        if self._task is not None:
            # Let an already-sent edit settle before sending the final text.
            self._stop.set()
            await self._task
            if self.progress.phase in TERMINAL_PHASES:
                await self._finalize()
            else:
                await self._edit()

    async def _run(self):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                return
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            await self._edit()

    async def _edit(self, *, deadline=None) -> bool:
        text = self.progress.text()
        clear_buttons = (
            self._clear_buttons_on_exit
            and not self._buttons_cleared
            and self.progress.phase in TERMINAL_PHASES
        )
        if text == self._last and not clear_buttons:
            return True
        if not self._editable or time.monotonic() < self._retry_at:
            return False
        try:
            edit_deadline = time.monotonic() + self.EDIT_TIMEOUT
            async with asyncio.timeout_at(
                edit_deadline if deadline is None else min(deadline, edit_deadline)
            ):
                options = {"buttons": None} if clear_buttons else {}
                await self.message.edit(text, parse_mode=None, **options)
            self._last = text
            self._buttons_cleared |= clear_buttons
        except errors.FloodWaitError as error:
            self._failure = error_fields(error)
            self._retry_at = time.monotonic() + max(0, error.seconds)
            return False
        except errors.MessageNotModifiedError:
            self._last = text
            self._buttons_cleared |= clear_buttons
        except Exception as error:
            # Status failures must not turn an accepted file into a failed transfer.
            self._failure = error_fields(error)
            self._editable = isinstance(error, (TimeoutError, OSError, errors.RPCError))
            if isinstance(error, (errors.BadRequestError, errors.ForbiddenError)):
                self._editable = False
            return False
        self._failure = {}
        return True

    async def _finalize(self):
        deadline = time.monotonic() + self.FINAL_TIMEOUT
        fields = {"transfer_id": self.progress.transfer_id, "phase": self.progress.phase}
        for attempt in range(self.FINAL_ATTEMPTS):
            delay = max(0, self._retry_at - time.monotonic())
            if attempt:
                delay = max(delay, self.RETRY_DELAY * attempt)
            if not self._editable or time.monotonic() + delay >= deadline:
                break
            if delay:
                await asyncio.sleep(delay)
            if await self._edit(deadline=deadline):
                log.info({"event": "transfer_status_finalized", **fields})
                return
            log.warning(
                {
                    "event": "transfer_status_retry",
                    **fields,
                    "attempt": attempt + 1,
                    **self._failure,
                }
            )
        log.warning({"event": "transfer_status_unconfirmed", **fields, **self._failure})
        if self._terminal_reply is not None:
            try:
                async with asyncio.timeout(self.EDIT_TIMEOUT):
                    await self._terminal_reply(self.progress.text())
                log.info({"event": "transfer_status_replied", **fields})
            except Exception as error:
                # Do not erase the only status when its replacement is unconfirmed.
                log.warning(
                    {"event": "transfer_status_reply_failed", **fields, **error_fields(error)}
                )
                return
        # A long edit cooldown must not leave an obsolete percentage or stop button.
        delete = getattr(self.message, "delete", None)
        if delete is not None:
            try:
                async with asyncio.timeout(self.EDIT_TIMEOUT):
                    await delete()
                log.info({"event": "transfer_status_removed", **fields})
            except Exception as error:
                log.warning(
                    {"event": "transfer_status_cleanup_failed", **fields, **error_fields(error)}
                )
