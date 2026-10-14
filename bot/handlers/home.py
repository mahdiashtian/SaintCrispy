import logging

from telethon import Button, errors

from bot.keyboards.inline import start_buttons
from bot.texts import help_text, sites_text, start_text
from core.diagnostics import error_fields
from core.request_context import user_request

log = logging.getLogger("activity")


async def handle_home(event, jobs=None, bot_username=None, interval=60):
    owner_id = int(event.pattern_match[1])
    if owner_id != event.sender_id:
        await event.answer("این منو برای تو نیست. /start رو بزن تا منوی خودت باز بشه.", alert=True)
        return
    action = event.pattern_match[2].decode("ascii")
    buttons = [[Button.inline("برگشت", f"home:{owner_id}:main")]]
    with user_request(event.sender_id, event.chat_id):
        log.info({"event": "home_action", "action": action})
        if action == "help":
            text = help_text(interval)
        elif action == "sites":
            text = sites_text()
        elif action == "active":
            active = jobs.for_owner(owner_id, event.chat_id) if jobs else ()
            text = "الان دانلود فعالی نداری. یه لینک بفرست تا شروع کنیم."
            if active:
                text = "درخواست‌های فعال تو:\n\n" + "\n\n".join(
                    f"{index}. {job.progress.text()}" for index, job in enumerate(active[:10], 1)
                )
                buttons = [
                    [Button.inline(f"توقف درخواست {index}", f"stop:{job.token}")]
                    for index, job in enumerate(active[:10], 1)
                    if job.progress.phase not in {"publishing", "saving", "done"}
                ] + buttons
                if len(active) > 10:
                    text += f"\n\n{len(active) - 10} درخواست دیگه هم فعاله."
            buttons.insert(0, [Button.inline("تازه‌سازی", f"home:{owner_id}:active")])
        else:
            text, buttons = start_text(bot_username, interval), start_buttons(owner_id)
        await event.answer()
        try:
            await event.edit(text, buttons=buttons, parse_mode=None)
        except errors.MessageNotModifiedError:
            pass
        except errors.RPCError as error:
            log.warning({"event": "home_reply_failed", **error_fields(error)})
