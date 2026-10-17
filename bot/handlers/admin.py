"""Private admin navigation, persistent prompts and explicit job confirmations."""

import io

from telethon import errors, events, types

from bot.keyboards.admin import back, button, compact_id, confirmation, home
from bot.texts.admin import HOME, PROMPTS, group_text, user_text
from schemas.media import DownloadError


def private(event):
    return (
        type(getattr(event, "sender_id", None)) is int
        and event.sender_id > 0
        and type(getattr(event, "chat_id", None)) is int
        and event.chat_id == event.sender_id
        and not getattr(event, "is_group", False)
    )


async def show(event, text, buttons, presentation=None):
    if getattr(event, "data", None) is not None:
        if presentation:
            await presentation.edit(
                event, text, buttons=buttons, parse_mode=None, link_preview=False
            )
        else:
            await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
    elif presentation:
        await presentation.respond(
            event, text, buttons=buttons, parse_mode=None, link_preview=False
        )
    else:
        await event.respond(text, buttons=buttons, parse_mode=None, link_preview=False)


async def open_panel(event, admin, presentation=None, conversations=None):
    if not private(event):
        return
    try:
        root = await admin.authorize(event.sender_id)
        if conversations is not None:
            from bot.admin_keyboard import locks, reply_event
            from bot.handlers.admin_reference import panel

            async with locks.hold(str(event.sender_id)):
                await admin.clear_prompts(event.sender_id)
                await panel(
                    reply_event(event, conversations, presentation),
                    admin,
                    conversations,
                    presentation=presentation,
                )
            raise events.StopPropagation
        await show(event, HOME, home(event.sender_id, root), presentation)
    except DownloadError as error:
        await event.respond(f"⛔️ {error}", parse_mode=None)


async def callback(event, admin, presentation=None, conversations=None):
    from bot.admin_keyboard import locks, reply_event
    from bot.handlers.admin_reference import Invocation

    if conversations is not None and not isinstance(event, Invocation) and private(event):
        if (getattr(event, "data", None) or b"").startswith(
            f"adm:{event.sender_id}:resetconfirm:".encode("ascii")
        ):
            # The maintenance gate serializes resets. Holding the owner lock here
            # would deadlock while draining another update waiting for that lock.
            return await _callback(
                reply_event(event, conversations, presentation), admin, presentation, conversations
            )
        async with locks.hold(str(event.sender_id)):
            return await _callback(
                reply_event(event, conversations, presentation), admin, presentation, conversations
            )
    return await _callback(event, admin, presentation, conversations)


async def _callback(event, admin, presentation=None, conversations=None):
    if not private(event):
        await event.answer("پنل مدیریت فقط در گفت‌وگوی خصوصی فعال است.", alert=True)
        return
    try:
        parts = event.data.decode("ascii").split(":")
        if len(parts) != 4 or parts[0] != "adm" or int(parts[1]) != event.sender_id:
            raise DownloadError("این دکمه متعلق به تو نیست.")
        owner, action, value = event.sender_id, parts[2], parts[3]
        root = await admin.authorize(owner)
        await event.answer()
        buttons = back(owner)
        if action == "home":
            if conversations is not None:
                from bot.handlers.admin_reference import panel

                await admin.clear_prompts(owner)
                await panel(event, admin, conversations, presentation=presentation)
                return
            text = HOME
            buttons = home(owner, root)
        elif action == "audience":
            from bot.handlers.admin_reference import ask
            from core.enums import ConversationState

            encoded, scope = value.split("_", 1)
            if (
                encoded not in {"b", "f"}
                or scope not in {"both", "admin", "non_admin"}
                or conversations is None
            ):
                raise DownloadError("مخاطبان معتبر نیستند.")
            await ask(
                event,
                admin,
                conversations,
                "exclusions",
                ConversationState.ADMIN_CAT_MSG_PANEL,
                mode="broadcast" if encoded == "b" else "forward",
                audience="groups",
                group_audience=scope,
            )
            return
        elif action == "dir":
            from bot.handlers.admin_reference import directory

            page, active, order = value.split("_", 2)
            await directory(event, admin, int(page), active, order)
            return
        elif action == "dirrefresh":
            from bot.handlers.admin_reference import directory

            active, order = value.split("_", 1)
            await directory(event, admin, 0, active, order)
            return
        elif action == "noop":
            return
        elif action == "dismiss":
            from bot.handlers.admin_render import source_reply
            from bot.texts.couplyo_admin import ADMIN_DANGER_CANCELLED

            await source_reply(event, ADMIN_DANGER_CANCELLED, buttons=None, edit=True)
            return
        elif action in {
            "cleanpick",
            "cleanfilter",
            "cleandefault",
            "cleanall",
            "cleango",
            "cleanclose",
        }:
            from bot.handlers.admin_maintenance import cleanup_callback

            await cleanup_callback(event, admin, action, value)
            return
        elif action == "mdir":
            from bot.handlers.admin_reference import member_directory

            chat, page, active, started = value.split("_")
            await member_directory(event, admin, int(chat, 36), int(page), active, started)
            return
        elif action == "crawl":
            token = await admin.prepare(owner, "crawl", {"chat_id": int(value)})
            text = f"خزش اعضای قابل مشاهدهٔ گروه {value} در صف پایدار ثبت شود؟"
            buttons = confirmation(owner, token)
        elif action == "nudge_target":
            await admin.authorize(owner, root=True)
            if value not in {"owner", "admins", "both"}:
                raise DownloadError("مخاطب یادآوری معتبر نیست.")
            from bot.handlers.admin_render import source_reply
            from bot.texts.couplyo_admin import admin_nudge_owners_ask_name

            buttons = [
                [button(owner, "🏷 با ذکرِ نامِ گروه", "nudge_go", value + "_yes")],
                [button(owner, "🙈 بدونِ نامِ گروه", "nudge_go", value + "_no")],
                [button(owner, "✖️ بی‌خیال", "home")],
            ]
            await source_reply(
                event, admin_nudge_owners_ask_name(value), buttons=buttons, edit=True
            )
            return
        elif action == "nudge_go":
            target, with_name = value.split("_")
            if target not in {"owner", "admins", "both"} or with_name not in {"yes", "no"}:
                raise DownloadError("تنظیم یادآوری معتبر نیست.")
            token = await admin.prepare(
                owner, "nudge_owners", {"target": target, "with_name": with_name == "yes"}
            )
            text = "ارسال یادآوری برای گروه‌های خارج‌شده را تأیید می‌کنی؟"
            buttons = confirmation(owner, token)
        elif action in {"resetprepare", "resetconfirm", "resetcancel"}:
            from bot.handlers.admin_maintenance import reset_callback

            await reset_callback(event, admin, action, value)
            return
        elif action in {"cleanview", "cleanprepare", "cleanup", "restore"}:
            from bot.handlers.admin_maintenance import decode, open_cleanup

            if action == "cleanview":
                await open_cleanup(event, admin, value=value)
                return
            if action == "cleanprepare":
                entity, filters = decode(value)
                token, preview = await admin.maintenance.prepare_cleanup(owner, entity, filters)
                text = f"⚠️ حذف حداکثر {preview['batch']} رکورد و عضویت‌های وابسته را تأیید می‌کنی؟"
                buttons = [
                    [
                        button(owner, "✅ تأیید حذف", "cleanup", token),
                        button(owner, "انصراف", "home"),
                    ]
                ]
            elif action == "cleanup":
                from bot.handlers.admin_render import source_reply
                from bot.texts.couplyo_admin import pd

                code, token = value.split(".", 1) if value.startswith(("u.", "g.")) else ("", value)
                count = await admin.maintenance.confirm_cleanup(owner, token)
                noun = {"u": "کاربر", "g": "گروه"}.get(code, "مورد")
                await source_reply(
                    event,
                    f"✅ حذف شد: <b>{pd(count)}</b> {noun} (+ هرچی cascade می‌شد).\n"
                    "هر چیزی که با فیلتر هم‌خوان نبود دست‌نخورده موند و کش هم‌گام شد.",
                    buttons=None,
                    edit=True,
                )
                return
            else:
                counts = await admin.maintenance.confirm_restore(owner, value)
                text = "✅ بازیابی افزایشی انجام شد؛ رکوردهای موجود حفظ شدند.\n" + "\n".join(
                    f"{key}: {count} ردیف بررسی شد" for key, count in counts.items()
                )
        elif action == "watchdel":
            from bot.handlers.admin_reference import watch_detail

            await watch_detail(event, admin, int(value), confirm=True)
            return
        elif action == "watchconfirm":
            from bot.texts.couplyo_admin import WATCH_REMOVED

            await admin.channels.remove_watch(owner, int(value))
            text = WATCH_REMOVED
        elif action in {"watchpage", "watch", "wkind"}:
            from bot.handlers.admin_reference import watch_detail, watch_page
            from bot.texts.admin_buttons import KINDS

            if action == "watchpage":
                await watch_page(event, admin, int(value))
            else:
                if action == "wkind":
                    encoded, kind = value.split("_", 1)
                    identity = int(encoded, 36)
                    if kind not in set(KINDS) | {"all", "none"}:
                        raise DownloadError("نوع پیام معتبر نیست.")

                    def toggle(rows):
                        for row in rows:
                            if row["chat_id"] == identity:
                                row["kinds"] = (
                                    list(KINDS)
                                    if kind == "all"
                                    else []
                                    if kind == "none"
                                    else sorted(set(row["kinds"]).symmetric_difference({kind}))
                                )
                        return rows

                    await admin.channels.modify(owner, "watches", toggle)
                else:
                    identity = int(value)
                await watch_detail(event, admin, identity)
            return
        elif action == "stats":
            data = await admin.stats(owner)
            labels = {
                "users": "کاربران",
                "started": "استارت خصوصی",
                "active_groups": "گروه‌های فعال",
                "admin_groups": "گروه‌های ادمین",
                "removed_groups": "گروه‌های خارج‌شده",
                "unknown_groups": "گروه‌های نامعلوم",
                "members": "عضویت‌های فعال",
                "messages": "پیام‌های ثبت‌شده",
                "views": "بازدید لینک‌ها",
                "deliveries": "ارسال فایل",
                "pending": "اثرهای در انتظار",
                "dead": "اثرهای ناموفق",
            }
            text = "📊 آمار ربات\n" + "\n".join(
                f"{labels[key]}: {value}" for key, value in data.items()
            )
        elif action == "daily":
            rows = await admin.daily(owner)
            text = "📅 تغییرات یکتای روزانه؛ روز تهران\n" + "\n".join(
                f"{row['day']}: ورود {row['joined']} · خروج {row['removed']} · ارتقا {row['admin_gained']} · تنزل {row['admin_lost']}"
                for row in rows
            )
        elif action in {"daily_summary", "stats_history"}:
            from bot.handlers.admin_render import source_reply
            from bot.texts import couplyo_admin as copy

            if action == "daily_summary":
                text = copy.downloader_daily_stats(await admin.daily_summary(owner))
            else:
                total = await admin.history_count(owner)
                pages = max(1, (total + 13) // 14)
                page = min(max(0, int(value)), pages - 1)
                rows = await admin.history(owner, limit=14, offset=page * 14)
                text = copy.downloader_daily_history(rows, page=page, pages=pages)
                buttons = [
                    [
                        button(
                            owner, "◀️ قبلی", "stats_history" if page else "noop", max(0, page - 1)
                        ),
                        button(
                            owner,
                            "بعدی ▶️",
                            "stats_history" if page + 1 < pages else "noop",
                            page + 1,
                        ),
                    ]
                ]
            await source_reply(event, text, buttons=buttons, presentation=presentation, edit=True)
            return
        elif action == "cache":
            status = await admin.cache_status(owner)
            text = f"🗄 کش قابل بازسازی\nفعال: {status['enabled']}\nدر دسترس: {status['reachable']}\nکلیدهای نیازمند ترمیم: {status['dirty']}\nوضعیت گفتگو و مجوزهای حساس از SQL خوانده می‌شوند."
        elif action == "users":
            rows = await admin.users(owner, int(value))
            text = "👤 کاربران ثبت‌شده\n" + "\n".join(
                f"{row.user_id} · {(row.first_name or row.username or 'کاربر')[:70]}"
                for row in rows
            )
            buttons = [[button(owner, str(row.user_id), "user", row.user_id)] for row in rows]
            if len(rows) == 20:
                buttons.append([button(owner, "صفحه بعد", "users", rows[-1].user_id)])
            buttons += back(owner)
        elif action == "groups":
            rows = await admin.groups_page(owner, None if value == "0" else int(value))
            text = "👥 گروه‌های ثبت‌شده؛ وضعیت‌ها از دیتابیس\nبرای جزئیات، گروه را انتخاب کن."
            buttons = [
                [button(owner, (row.title or str(row.chat_id))[:45], "group", row.chat_id)]
                for row in rows
            ]
            if len(rows) == 20:
                buttons.append([button(owner, "صفحه بعد", "groups", rows[-1].chat_id)])
            buttons += back(owner)
        elif action == "user":
            user = await admin.user(owner, int(value))
            if not user:
                raise DownloadError("این کاربر ثبت نشده است.")
            text = user_text(user)
            buttons = [[button(owner, "📜 فایل‌های دریافت‌شده", "trace", user.user_id)]] + buttons
        elif action == "trace":
            from bot.handlers.download_history import show_trace

            await show_trace(
                event,
                await admin.public_trace(owner, int(value)),
                presentation=presentation,
                edit=True,
            )
            return
        elif action == "group":
            group = await admin.group(owner, int(value))
            if not group:
                raise DownloadError("این گروه ثبت نشده است.")
            from bot.handlers.admin_render import source_reply
            from bot.texts.couplyo_admin import group_card, group_info_page

            text = group_info_page(group_card(group))
            buttons = [
                [
                    button(
                        owner, "👥 اعضای گروه", "mdir", f"{compact_id(group.chat_id)}_0_all_all"
                    ),
                    button(owner, "🩺 بررسی زنده", "health", group.chat_id),
                ]
            ]
            if root:
                buttons.append(
                    [
                        button(owner, "🕷 خزش اعضا", "crawl", group.chat_id),
                        button(owner, "🚪 خروج ربات", "leave", group.chat_id),
                    ]
                )
            buttons += back(owner)
            await source_reply(event, text, buttons=buttons, presentation=presentation, edit=True)
            return
        elif action == "members":
            chat, cursor = (int(part, 36) for part in value.split("_"))
            rows = await admin.members(owner, chat, cursor)
            text = "👥 اعضای انسانی مشاهده‌شده؛ این فهرست ادعای خزش کامل ندارد.\n" + "\n".join(
                f"{row['user_id']} · {(row['first_name'] or row['username'] or 'کاربر')[:50]} · {'عضو' if row['is_active'] else 'خارج‌شده'} · {row['message_count']} پیام"
                for row in rows
            )
            if len(rows) == 20:
                buttons = [
                    [
                        button(
                            owner,
                            "صفحه بعد",
                            "members",
                            f"{compact_id(chat)}_{compact_id(rows[-1]['user_id'])}",
                        )
                    ]
                ] + buttons
        elif action in {"exportu", "exportg"}:
            kind = "users" if action == "exportu" else "groups"
            result = await admin.export(owner, kind, None if value == "0" else int(value))
            data = io.BytesIO(result.data)
            data.name = result.filename
            await event.respond("📁 خروجی همین بخش؛ حداکثر ۵۰۰ ردیف.", file=data, parse_mode=None)
            text = "فایل خروجی آماده شد."
            if result.next_cursor is not None:
                buttons = [[button(owner, "خروجی بخش بعد", action, result.next_cursor)]] + buttons
        elif action == "jobs":
            rows = await admin.jobs(owner)
            text = "🧾 عملیات مدیریتی\n" + "\n".join(
                f"#{row.id} · {row.kind} · {row.status}\nکل {row.total_count} · موفق {row.success_count} · ناموفق {row.failure_count} · ردشده {row.skipped_count}"
                for row in rows
            )
            buttons = (
                [
                    [button(owner, f"توقف #{row.id}", "cancel", row.id)]
                    for row in rows
                    if row.status in {"queued", "running"}
                ]
                if root
                else []
            ) + buttons
        elif action == "cancel":
            text = (
                "⏹ درخواست توقف ثبت شد."
                if await admin.cancel(owner, int(value))
                else "این عملیات دیگر فعال نیست."
            )
        elif action in {"health", "leave"}:
            payload = {} if value == "0" else {"chat_id": int(value)}
            token = await admin.prepare(owner, action, payload)
            text = (
                "عملیات بررسی سلامت گروه‌ها" if action == "health" else f"خروج ربات از گروه {value}"
            )
            text += "\nاجرای این عملیات را تأیید می‌کنی؟"
            buttons = confirmation(owner, token)
        elif action == "confirm":
            identity = await admin.confirm(owner, value)
            text = f"✅ عملیات #{identity} در صف پایدار ثبت شد. پیشرفت را از «عملیات» ببین."
        elif action == "settings":
            if conversations is not None:
                from bot.handlers.admin_reference import download_settings

                await download_settings(event, admin, conversations)
                return
            options = await admin.options(owner)
            if not options["root"]:
                raise DownloadError("تنظیمات فقط برای مدیر اصلی است.")
            text = "⚙️ تنظیمات زنده\nتغییرها در دیتابیس و سابقهٔ مدیریت ثبت می‌شوند."
            buttons = [
                [
                    button(
                        owner,
                        "دریافت فایل: " + ("روشن" if options["downloads_enabled"] else "خاموش"),
                        "pause",
                        int(not options["downloads_enabled"]),
                    )
                ],
                [
                    button(
                        owner,
                        "اعلان گروه: "
                        + ("روشن" if options["lifecycle_notifications"] else "خاموش"),
                        "notifications",
                        int(not options["lifecycle_notifications"]),
                    )
                ],
            ] + buttons
        elif action in {"pause", "notifications"}:
            if value not in {"0", "1"}:
                raise DownloadError("تنظیم معتبر نیست.")
            await admin.mutate(owner, action, value=value == "1")
            text = "✅ تنظیم ذخیره شد."
        elif action in {"prompt", "compose"}:
            form = value
            payload = {}
            if action == "compose":
                if value not in {"busers", "bgroups", "fusers", "fgroups"}:
                    raise DownloadError("مخاطبان معتبر نیستند.")
                form = "broadcast" if value.startswith("b") else "forward"
                payload = {"audience": "users" if value.endswith("users") else "groups"}
            token = await admin.prompt(owner, form, payload)
            message = await event.respond(
                PROMPTS[form],
                parse_mode=None,
                buttons=types.ReplyKeyboardForceReply(single_use=True, selective=True),
            )
            await admin.attach_prompt(owner, token, message.id)
            return
        else:
            raise DownloadError("دکمه معتبر نیست.")
        await show(event, text or "چیزی ثبت نشده است.", buttons, presentation)
    except errors.MessageNotModifiedError:
        pass
    except (DownloadError, ValueError, UnicodeError, KeyError) as error:
        text = str(error) if isinstance(error, DownloadError) else "ورودی مدیریت معتبر نیست."
        await event.answer(text, alert=True)


async def input_reply(event, admin, presentation=None, conversations=None):
    if not private(event) or not getattr(event, "reply_to_msg_id", None):
        return
    if conversations is not None:
        from bot.admin_keyboard import reply_event

        event = reply_event(event, conversations, presentation)
    raw = (getattr(event, "raw_text", "") or "").strip()
    command = raw.split(maxsplit=1)[0].split("@", 1)[0] if raw else ""
    if command == "/help" or raw.removeprefix("📖").strip() == "راهنما":
        return
    if command in {"/start", "/admin"}:
        await admin.clear_prompts(event.sender_id)
        return
    handled = False
    try:
        prompt = await admin.find_prompt(event.sender_id, event.reply_to_msg_id)
        if prompt is None:
            return
        handled = True
        action, payload = await admin.consume_prompt(event.sender_id, prompt["token"])
        owner = event.sender_id
        raw = event.raw_text.strip()
        buttons = back(owner)
        if action == "import_users":
            size = getattr(getattr(event, "file", None), "size", None)
            if type(size) is not int or size > 1024 * 1024:
                raise DownloadError("فایل CSV با اندازه مشخص و حداکثر یک مگابایت بفرست.")
            content = await event.download_media(bytes)
            count = await admin.import_users(owner, content)
            text = f"✅ {count} ردیف وارد شد؛ داده‌های مجوز و استارت تغییر نکردند."
        elif action == "lookup":
            user = await admin.user(owner, int(raw) if raw.isdigit() else raw)
            if not user:
                raise DownloadError("کاربر پیدا نشد.")
            text = user_text(user)
        elif action == "group_lookup":
            group = await admin.group(owner, int(raw))
            if not group:
                raise DownloadError("گروه پیدا نشد.")
            text = group_text(group)
        elif action in {"broadcast", "forward"}:
            if action == "broadcast":
                excluded = []
                if raw.startswith("استثنا:"):
                    header, _, raw = raw.partition("\n")
                    excluded = [
                        int(value.strip())
                        for value in header.partition(":")[2].split(",")
                        if value.strip()
                    ]
                payload = {**payload, "text": raw, "exclude": excluded}
            else:
                payload = {**payload, "source_id": event.id}
            token = await admin.prepare(owner, action, payload)
            audience = (
                "کاربران دارای استارت خصوصی"
                if payload["audience"] == "users"
                else "گروه‌های فعال با امکان ارسال"
            )
            text = f"📨 مخاطبان: {audience}\nشروع ارسال را تأیید می‌کنی؟"
            buttons = confirmation(owner, token)
        else:
            values = raw.split()
            await admin.mutate(
                owner, action, int(values[0]), int(values[1]) if action == "limit" else None
            )
            text = "✅ تغییر ذخیره و در سابقهٔ مدیریت ثبت شد."
        await show(event, text, buttons, presentation)
    except DownloadError as error:
        if error.code != "admin_denied":
            await event.respond(f"⚠️ {error}", parse_mode=None)
    except (ValueError, IndexError):
        await event.respond("⚠️ ورودی معتبر نیست؛ فرم را دوباره از پنل باز کن.", parse_mode=None)
    finally:
        if handled:
            raise events.StopPropagation
