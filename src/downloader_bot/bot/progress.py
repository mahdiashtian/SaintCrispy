import asyncio
import time
from contextlib import suppress

from telethon import errors, types

from downloader_bot.schemas.transfer import TransferState


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
    """Edit at most once per interval, without slowing the media transfer."""

    def __init__(
        self,
        message,
        progress: TransferProgress,
        interval: float = 2.5,
        clear_buttons_on_exit: bool = False,
    ):
        self.message = message
        self.progress = progress
        self.interval = interval
        self._last = progress.text()
        self._retry_at = 0.0
        self._task = None
        self._clear_buttons_on_exit = clear_buttons_on_exit
        self._buttons_cleared = False

    async def __aenter__(self):
        if self.message is not None:
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *args):
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            await self._edit()

    async def _run(self):
        while True:
            await asyncio.sleep(self.interval)
            await self._edit()

    async def _edit(self):
        text = self.progress.text()
        clear_buttons = (
            self._clear_buttons_on_exit
            and not self._buttons_cleared
            and self.progress.phase in {"done", "error", "cancelled"}
        )
        if (text == self._last and not clear_buttons) or time.monotonic() < self._retry_at:
            return
        try:
            async with asyncio.timeout(3):
                options = {"buttons": types.ReplyInlineMarkup(rows=[])} if clear_buttons else {}
                await self.message.edit(text, parse_mode=None, **options)
            self._last = text
            self._buttons_cleared |= clear_buttons
        except errors.FloodWaitError as error:
            self._retry_at = time.monotonic() + error.seconds
        except errors.MessageNotModifiedError:
            self._last = text
            self._buttons_cleared |= clear_buttons
        except (errors.RPCError, TimeoutError, OSError):
            # A deleted status message or an edit failure must not abort the transfer.
            pass
