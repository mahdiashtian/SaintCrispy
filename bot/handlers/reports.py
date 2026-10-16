"""Fresh group-admin authorization for source daily/weekly report commands."""

import asyncio

from telethon import errors


async def handle_report(event, *, management, client):
    if not getattr(event, "is_group", False):
        return
    owner = event.sender_id
    root = owner == management.admin.main_admin_id
    if not root:
        try:
            async with asyncio.timeout(5):
                permissions = await client.get_permissions(event.chat_id, owner)
            if not (permissions.is_admin or permissions.is_creator):
                await event.respond("این دستور فقط برای ادمین‌های گروه است ⛔️", reply_to=event.id)
                return
        except (errors.RPCError, ValueError, OSError, TimeoutError):
            await event.respond(
                "نتونستم دسترسی ادمینی رو بررسی کنم؛ کمی بعد دوباره امتحان کن.", reply_to=event.id
            )
            return
    period = "day" if event.raw_text.split("@", 1)[0] == "/dailyreport" else "week"
    if await management.request_report(owner, event.chat_id, period, event.id):
        await event.respond("گزارش در حال آماده‌سازی است… 📊", reply_to=event.id)
    else:
        await event.respond(
            "گزارش این بازه قبلاً درخواست شده؛ ۲۴ ساعت بعد دوباره امتحان کن.", reply_to=event.id
        )
