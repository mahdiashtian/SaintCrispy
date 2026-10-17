import logging

from telethon import Button, errors

from bot.keyboards.inline import start_buttons
from bot.texts import sites_text, start_text
from core.diagnostics import error_fields
from core.request_context import user_request

from .help import help_view

log = logging.getLogger("activity")


async def handle_home(
    event,
    jobs=None,
    bot_username=None,
    interval=60,
    presentation=None,
    access=None,
    management=None,
    group_limit=5,
    quota_limits=None,
):
    owner_id = int(event.pattern_match[1])
    if owner_id != event.sender_id:
        await event.answer("این منو برای تو نیست. /start رو بزن تا منوی خودت باز بشه.", alert=True)
        return
    action = event.pattern_match[2].decode("ascii")
    if event.chat_id < 0 and action != "help":
        await event.answer(
            "منوی اصلی فقط در پیوی باز می‌شه؛ برای راهنمای گروه /help رو بفرست.", alert=True
        )
        return
    youtube_enabled = access is None or access.allows("youtube", event.sender_id)
    buttons = [[Button.inline("برگشت", f"home:{owner_id}:main")]]
    with user_request(event.sender_id, event.chat_id):
        log.info({"event": "home_action", "action": action})
        if action == "help":
            text, buttons = await help_view(
                owner_id,
                bot_username=bot_username,
                interval=interval,
                group_limit=group_limit,
                access=access,
                management=management,
                quota_limits=quota_limits,
            )
        elif action == "sites":
            text = sites_text(youtube_enabled=youtube_enabled)
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
            limits = await quota_limits() if quota_limits is not None else None
            text, buttons = (
                start_text(
                    bot_username,
                    interval,
                    youtube_enabled=youtube_enabled,
                    user_limit=limits["user_limit"] if limits else None,
                ),
                start_buttons(owner_id),
            )
        if management is not None and action == "main":
            credit = await management.developer_credit()
            if credit:
                text += "\n\n" + credit
        await event.answer()
        try:
            if presentation:
                await presentation.edit(event, text, buttons=buttons)
            else:
                await event.edit(text, buttons=buttons, parse_mode=None)
        except errors.MessageNotModifiedError:
            pass
        except errors.RPCError as error:
            log.warning({"event": "home_reply_failed", **error_fields(error)})
