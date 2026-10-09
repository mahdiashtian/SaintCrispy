import asyncio
import logging

import asyncpg
import httpx
from telethon import Button, errors

from downloader_bot.bot.presentation import PresentedEvent
from downloader_bot.bot.progress import ProgressReporter, TransferProgress
from downloader_bot.bot.state.states import ConversationState
from downloader_bot.core.request_context import user_request
from downloader_bot.schemas.media import DownloadError
from downloader_bot.services.observability import error_fields

log = logging.getLogger("activity")


async def handle_quality(
    event, service, menus, jobs=None, limits=None, conversations=None, presentation=None
) -> None:
    if presentation:
        event = PresentedEvent(event, presentation)
    token = event.pattern_match[1].decode("ascii")
    index = int(event.pattern_match[2])
    try:
        menu = await menus.fetch(token, event.sender_id, event.chat_id)
        if index >= len(menu.media.qualities):
            raise DownloadError("کیفیت انتخاب‌شده معتبر نیست.")
    except DownloadError as error:
        await event.answer(str(error), alert=True)
        return
    except (asyncpg.PostgresError, OSError, TimeoutError) as error:
        log.error({"event": "menu_storage_failed", **error_fields(error)})
        await event.answer("⚠️ پایگاه داده در دسترس نیست؛ کمی بعد امتحان کن.", alert=True)
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
        if conversations:
            await conversations.set(
                event.sender_id,
                event.chat_id,
                ConversationState.TRANSFERRING,
                transfer_id=progress.transfer_id,
                menu_token=token,
            )
        await event.answer("📥 دریافت فایل شروع شد…")
        try:
            options = (
                {"buttons": [[Button.inline("توقف ⏹", data=f"stop:{job.token}")]]} if job else {}
            )
            status = await event.respond(
                progress.text(), parse_mode=None, reply_to=menu.message_id, **options
            )
        except errors.RPCError:
            status = None
    except BaseException:
        if job and not job.cancelled:
            jobs.cancel(job.token, event.sender_id, event.chat_id)
        if conversations:
            await conversations.finish_transfer(
                event.sender_id, event.chat_id, progress.transfer_id, ConversationState.FAILED
            )
        raise

    async def transfer():
        await run_transfer(event, service, menu, index, status, progress, conversations)

    if job:
        if not jobs.start(job, transfer):
            async with ProgressReporter(status, progress, clear_buttons_on_exit=True):
                pass
    else:
        # Compatibility for callers that do not own a task registry.
        await transfer()


async def run_transfer(event, service, menu, index, status, progress, conversations=None) -> None:
    async with ProgressReporter(status, progress, clear_buttons_on_exit=True):
        try:
            peer = await event.get_input_chat()
            with user_request(
                event.sender_id, event.chat_id, progress.transfer_id, menu.message_id
            ):
                await service.deliver(peer, menu.media, menu.media.qualities[index], progress)
        except asyncio.CancelledError:
            progress.phase = "cancelled"
            if conversations:
                await conversations.finish_transfer(
                    event.sender_id,
                    event.chat_id,
                    progress.transfer_id,
                    ConversationState.CANCELLED,
                )
            raise
        except DownloadError as error:
            progress.error = str(error)
        except errors.FloodWaitError as error:
            progress.error = f"تلگرام محدودیت موقت اعمال کرده؛ {error.seconds} ثانیه بعد امتحان کن."
        except asyncpg.PostgresError:
            progress.error = "پایگاه داده در دسترس نیست؛ درخواست را کمی بعد تکرار کن."
        except (httpx.HTTPError, TimeoutError, errors.RPCError):
            progress.error = "ارسال فایل کامل نشد؛ درخواست را دوباره امتحان کن."
        except Exception as error:
            log.error(
                {
                    "event": "transfer_handler_failed",
                    "transfer_id": progress.transfer_id,
                    **error_fields(error),
                }
            )
            progress.error = "انتقال به علت خطای داخلی کامل نشد؛ دوباره امتحان کن."
        if progress.error:
            progress.phase = "error"
            if status is None:
                await event.respond(progress.error, parse_mode=None)
        if conversations:
            await conversations.finish_transfer(
                event.sender_id,
                event.chat_id,
                progress.transfer_id,
                ConversationState.FAILED if progress.error else ConversationState.DONE,
            )
