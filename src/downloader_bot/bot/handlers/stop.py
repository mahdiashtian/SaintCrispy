import asyncio

from telethon import errors, types

from downloader_bot.schemas.media import DownloadError


async def handle_stop(event, jobs) -> None:
    if jobs is None:
        await event.answer("صف انتقال در این اجرا فعال نیست.", alert=True)
        return
    token = event.pattern_match[1].decode("ascii")
    try:
        job = jobs.get(token, event.sender_id, event.chat_id)
        stopped = jobs.cancel(token, event.sender_id, event.chat_id)
    except DownloadError as error:
        await event.answer(str(error), alert=True)
        return
    if not stopped:
        await event.answer("ثبت نهایی ارسال آغاز شده؛ این مرحله قابل لغو نیست.", alert=True)
        return
    await event.answer("درخواست توقف پذیرفته شد.")
    try:
        async with asyncio.timeout(3):
            await event.edit(
                job.progress.text(),
                parse_mode=None,
                buttons=types.ReplyInlineMarkup(rows=[]),
            )
    except (errors.RPCError, TimeoutError, OSError):
        # Deleting the status message must not prevent cancellation.
        pass
