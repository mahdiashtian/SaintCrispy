"""A durable private crawl report edited in place, as in the Couplyo source."""

import asyncio
import html
import logging
import time

from telethon import errors

from bot.handlers.admin_render import source_reply
from core.diagnostics import error_fields
from core.jalali import to_persian_digits as pd

log = logging.getLogger("activity")


def progress_text(summary, recent):
    pending, failed = summary["pending"], summary["failed"]
    title = (
        "🔄 کرالِ کاملِ اعضای گروه‌ها در حالِ اجراست..."
        if pending
        else (
            "⚠️ کرال اعضای گروه‌ها با خطا پایان یافت"
            if failed
            else "✅ کرالِ کاملِ اعضای گروه‌ها تمام شد"
        )
    )
    lines = [
        title,
        "",
        f"گروهِ کرال‌شده: {pd(summary['done'])}",
        f"در صفِ انتظار: {pd(pending)} گروه",
        f"عضوِ خونده‌شده: {pd(summary['seen_unique'])}",
        f"عضوِ تازه‌اضافه‌شده: {pd(summary['stored_new'])}",
        f"بات و اکانتِ حذف‌شده (ذخیره نشد): {pd(summary['skipped_accounts'])}",
    ]
    states = {
        "queued": "در صف",
        "running": "در حال اجرا",
        "paused": "متوقف موقت",
        "done": "پایان‌یافته",
        "failed": "ناموفق",
        "cancelled": "لغوشده",
    }
    reasons = {
        "flood_wait": "محدودیت نرخ تلگرام",
        "not_admin": "ربات ادمین نیست",
        "converged": "پایان پیمایش",
        "budget_exhausted": "سقف درخواست",
        "max_passes": "سقف پیمایش",
        "basic_group_complete": "فهرست کامل گروه",
        "group_gone": "خروج از گروه",
    }
    if recent:
        lines += ["", "<b>آخرین گروه‌ها</b>"]
    for row in recent:
        total = pd(row["claimed_total"]) if row["claimed_total"] is not None else "نامعلوم"
        reason = reasons.get(row["stopped_because"], "")
        lines.append(
            f"• «{html.escape(row['title'] or str(row['chat_id']))}»: {pd(row['seen_unique'])} از {total} عضو · {pd(row['skipped_accounts'])} بات/حذف‌شده · {states[row['status']]}"
            + (f" ({reason})" if reason else "")
        )
    return "\n".join(lines)


class RosterProgress:
    def __init__(self, client, service, owner, *, presentation=None, rate=None, interval=60):
        self.client, self.service, self.owner = client, service, owner
        self.presentation, self.rate, self.interval = presentation, rate, interval
        self.lock = asyncio.Lock()
        self.last_at, self.last_phase, self.last_text = 0, None, None
        self.retry_at = 0

    async def update(self):
        if not self.owner:
            return
        async with self.lock:
            if time.monotonic() < self.retry_at:
                return
            try:
                async with asyncio.timeout(5):
                    summary = await self.service.summary()
                    if not summary["total"]:
                        return
                    phase = (bool(summary["pending"]), bool(summary["failed"]))
                    if phase == self.last_phase and time.monotonic() - self.last_at < self.interval:
                        return
                    recent = await self.service.recent()
                    text = progress_text(summary, recent)
                    if text == self.last_text:
                        return
                    saved = await self.service.report_state()
                    if self.rate:
                        delay = await self.rate.reserve()
                        if delay > 0:
                            self.retry_at = time.monotonic() + delay
                            return
                    message_id = (
                        saved.get("message_id") if saved.get("owner") == self.owner else None
                    )

                    async def respond(*args, **options):
                        return await self.client.send_message(self.owner, *args, **options)

                    async def edit(*args, **options):
                        return await self.client.edit_message(
                            self.owner, message_id, *args, **options
                        )

                    from types import SimpleNamespace

                    try:
                        message = await source_reply(
                            SimpleNamespace(respond=respond, edit=edit),
                            text,
                            presentation=self.presentation,
                            edit=bool(message_id),
                        )
                    except errors.MessageNotModifiedError:
                        message = None
                    except errors.MessageIdInvalidError:
                        message = await source_reply(
                            SimpleNamespace(respond=respond), text, presentation=self.presentation
                        )
                    if message is not None and not message_id:
                        await self.service.report_state(
                            {"owner": self.owner, "message_id": message.id}
                        )
                    elif message is not None and message.id != message_id:
                        await self.service.report_state(
                            {"owner": self.owner, "message_id": message.id}
                        )
                    self.last_at, self.last_phase, self.last_text = time.monotonic(), phase, text
            except errors.FloodWaitError as error:
                self.retry_at = time.monotonic() + max(1, error.seconds)
                if self.rate:
                    await self.rate.pause(error.seconds)
            except Exception as error:
                log.warning({"event": "roster_progress_failed", **error_fields(error)})
                self.retry_at = time.monotonic() + self.interval
