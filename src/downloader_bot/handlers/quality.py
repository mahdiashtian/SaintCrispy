import asyncio

import asyncpg
import httpx
from telethon import Button, errors

from downloader_bot.models import DownloadError
from downloader_bot.progress import ProgressReporter, TransferProgress
from downloader_bot.request_context import user_request


async def handle_quality(event, service, menus, jobs=None, limits=None) -> None:
    token = event.pattern_match[1].decode("ascii")
    index = int(event.pattern_match[2])
    try:
        menu = menus.get(token, event.sender_id, event.chat_id)
        if index >= len(menu.media.qualities):
            raise DownloadError("کیفیت انتخاب‌شده معتبر نیست.")
    except DownloadError as error:
        await event.answer(str(error), alert=True)
        return
    progress = TransferProgress()
    try:
        job = jobs.reserve(event.sender_id, event.chat_id, progress) if jobs is not None else None
    except DownloadError as error:
        await event.answer(str(error), alert=True)
        return
    try:
        if limits is not None:
            try:
                await limits.check(event.sender_id, "download")
            except (DownloadError, asyncpg.PostgresError, OSError, TimeoutError) as error:
                if job:
                    jobs.cancel(job.token, event.sender_id, event.chat_id)
                message = (
                    str(error)
                    if isinstance(error, DownloadError)
                    else "پایگاه داده در دسترس نیست؛ کمی بعد امتحان کن."
                )
                await event.answer(message, alert=True)
                return
        await event.answer("در حال شروع دریافت و ارسال…")
        try:
            options = (
                {"buttons": [[Button.inline("توقف ⏹", data=f"stop:{job.token}")]]} if job else {}
            )
            status = await event.respond(progress.text(), parse_mode=None, **options)
        except errors.RPCError:
            status = None
    except BaseException:
        if job and not job.cancelled:
            jobs.cancel(job.token, event.sender_id, event.chat_id)
        raise

    async def transfer():
        await run_transfer(event, service, menu, index, status, progress)

    if job:
        if not jobs.start(job, transfer):
            async with ProgressReporter(status, progress, clear_buttons_on_exit=True):
                pass
    else:
        # Compatibility for callers that do not own a task registry.
        await transfer()


async def run_transfer(event, service, menu, index, status, progress) -> None:
    async with ProgressReporter(status, progress, clear_buttons_on_exit=True):
        try:
            peer = await event.get_input_chat()
            with user_request(event.sender_id):
                await service.deliver(peer, menu.media, menu.media.qualities[index], progress)
        except asyncio.CancelledError:
            progress.phase = "cancelled"
            raise
        except DownloadError as error:
            progress.error = str(error)
        except errors.FloodWaitError as error:
            progress.error = f"تلگرام محدودیت موقت اعمال کرده؛ {error.seconds} ثانیه بعد امتحان کن."
        except asyncpg.PostgresError:
            progress.error = "پایگاه داده در دسترس نیست؛ درخواست را کمی بعد تکرار کن."
        except (httpx.HTTPError, TimeoutError, errors.RPCError):
            progress.error = "ارسال فایل کامل نشد؛ درخواست را دوباره امتحان کن."
        except Exception:
            progress.error = "انتقال به علت خطای داخلی کامل نشد؛ دوباره امتحان کن."
        if progress.error:
            progress.phase = "error"
            if status is None:
                await event.respond(progress.error, parse_mode=None)
