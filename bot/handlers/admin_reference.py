"""State-routed private administration, adapted from the local Couplyo panel."""

import io

from telethon import events, types, utils

from bot.keyboards import admin_reply as keys
from bot.keyboards.admin import button, compact_id, confirmation
from bot.texts import admin_buttons as b
from bot.texts.admin import HOME, PROMPTS
from core.enums import ConversationState as CS
from schemas.media import DownloadError

CATEGORIES = {
    b.ADMINS: CS.ADMIN_CAT_ADMINS_PANEL,
    b.MESSAGING: CS.ADMIN_CAT_MSG_PANEL,
    b.GROUPS: CS.ADMIN_GROUPS_PANEL,
    b.CHANNELS: CS.ADMIN_CAT_CHANNELS_PANEL,
    b.DATA: CS.ADMIN_CAT_DATA_PANEL,
    b.CLEANUP: CS.ADMIN_CAT_CLEANUP_PANEL,
}
SUBMENUS = {
    b.JOIN: CS.ADMIN_JOIN_PANEL,
    b.ARCHIVE: CS.ADMIN_ARCHIVE_PANEL,
    b.STORAGE: CS.ADMIN_STORAGE_PANEL,
    b.WATCH: CS.ADMIN_WATCH_PANEL,
}
PARENTS = {value: CS.ADMIN_PANEL for value in CATEGORIES.values()} | {
    value: CS.ADMIN_CAT_CHANNELS_PANEL for value in SUBMENUS.values()
}
PARENTS |= {
    CS.ADMIN_PROMO_PANEL: CS.ADMIN_CAT_CHANNELS_PANEL,
    CS.ADMIN_NUDGE_PANEL: CS.ADMIN_CAT_CHANNELS_PANEL,
    CS.ADMIN_WEEKLY_REPORT_PANEL: CS.ADMIN_CAT_CHANNELS_PANEL,
}


class Invocation:
    """Reuse existing owner-scoped read/confirmation actions from a reply menu."""

    def __init__(self, event, action, value="0"):
        self.event = event
        self.data = f"adm:{event.sender_id}:{action}:{value}".encode("ascii")

    def __getattr__(self, key):
        return getattr(self.event, key)

    async def answer(self, text=None, **kwargs):
        if text:
            await self.event.respond(text, parse_mode=None)

    async def edit(self, *args, **kwargs):
        return await self.event.respond(*args, **kwargs)


async def panel(event, admin, conversations, state=CS.ADMIN_PANEL, presentation=None):
    await admin.authorize(event.sender_id)
    await conversations.set_state(event.sender_id, event.chat_id, state)
    title = (
        HOME
        if state is CS.ADMIN_PANEL
        else next(
            (label for label, value in CATEGORIES.items() | SUBMENUS.items() if value is state),
            "مدیریت ربات",
        )
    )
    if state is CS.ADMIN_PANEL:
        identity = (await admin.channels.get(event.sender_id))["developer_id"]
        if identity:
            title += f"\n\n👨‍💻 توسعه‌دهنده: tg://user?id={identity}"
    if presentation:
        await presentation.respond(event, title, buttons=keys.menu(state), parse_mode=None)
    else:
        await event.respond(title, buttons=keys.menu(state), parse_mode=None)


async def ask(event, admin, conversations, action, parent, **data):
    await admin.authorize(
        event.sender_id,
        root=action not in {"lookup", "trace", "group_lookup", "members", "crawl_status"},
    )
    await conversations.set_state(
        event.sender_id, event.chat_id, CS.ADMIN_INPUT, action=action, parent=parent.value, **data
    )
    text = PROMPTS.get(action) or {
        "trace": "شناسه عددی یا نام کاربری را بفرست.",
        "members": "شناسه منفی گروه را بفرست.",
        "leave": "شناسه منفی گروهی که ربات از آن خارج شود را بفرست.",
        "crawl_status": "شناسه منفی گروه را بفرست.",
        "developer_id": "شناسه عددی توسعه‌دهنده را بفرست؛ برای حذف، صفر بفرست.",
        "target": "شناسه عددی مقصد را بفرست.",
        "exclusions": "شناسه‌های گروه‌های مستثنا را با فاصله یا کاما بفرست، یا «بدون استثنا» را بزن.",
        "exclude_adders": "شناسه کاربران آورندهٔ گروه را برای استثنا بفرست، یا «بدون استثنا» را بزن.",
        "channel_add": "آیدی عمومی کانال یا شناسه عددی و لینک دعوتش را بفرست؛ ربات باید به کانال دسترسی داشته باشد.",
        "channel_remove": "شناسه عددی کانال را بفرست.",
        "archive": "آیدی کانال آرشیو را بفرست.",
        "storage": "آیدی کانال ذخیره‌سازی را بفرست.",
        "watch_group": "شناسه منفی گروه مورد رصد را بفرست.",
        "watch_channel": "آیدی کانال مقصد رصد را بفرست.",
        "promo_text": "متن پیام تبلیغی گروه‌ها را بفرست (حداکثر ۳۵۰۰ نویسه).",
        "restore": "فایل JSON بک‌آپ داده‌های همین ربات را بفرست؛ پس از بررسی، تأیید جدا می‌گیری.",
    }.get(action, "ورودی را بفرست.")
    await event.respond(
        text,
        buttons=keys.EXCLUSIONS if action in {"exclusions", "exclude_adders"} else keys.BACK_MENU,
        parse_mode=None,
    )


async def invoke(event, admin, action, value="0", presentation=None):
    from bot.handlers.admin import callback

    await callback(Invocation(event, action, value), admin, presentation)


def parse_ids(raw, *, positive=None):
    if raw == b.SKIP:
        return []
    values = raw.replace(",", " ").split()
    if len(values) > 1000:
        raise DownloadError("حداکثر هزار استثنا وارد کن.")
    try:
        result = list(dict.fromkeys(int(value) for value in values))
    except ValueError as error:
        raise DownloadError("شناسه‌ها باید عددی باشند.") from error
    if any(
        value == 0
        or not -(2**63) < value < 2**63
        or (positive is True and value < 0)
        or (positive is False and value > 0)
        for value in result
    ):
        raise DownloadError("شناسه‌ها معتبر نیستند.")
    return result


async def parse_channel(event):
    pieces = (event.raw_text or "").strip().split()
    if not pieces:
        raise DownloadError("کانال را مشخص کن.")
    query = int(pieces[0]) if pieces[0].lstrip("-").isdigit() else pieces[0]
    client = event.client
    entity = await client.get_entity(query)
    if not isinstance(entity, types.Channel) or not entity.broadcast or entity.megagroup:
        raise DownloadError("یک کانال تلگرام معتبر بفرست.")
    rights = await client.get_permissions(entity, "me")
    if not getattr(rights, "is_admin", False):
        raise DownloadError("ربات را ابتدا در کانال ادمین کن.")
    link = (
        f"https://t.me/{entity.username}"
        if entity.username
        else (pieces[1] if len(pieces) > 1 else "")
    )
    if not link.startswith("https://t.me/"):
        raise DownloadError("برای کانال خصوصی، شناسه عددی و لینک دعوت را با فاصله بفرست.")
    return {"id": utils.get_peer_id(entity), "link": link, "title": (entity.title or "")[:256]}


async def directory(event, admin, page=0, active="all", order="date", *, edit=False):
    rows, total, page = await admin.group_directory(
        event.sender_id, page, {"all": None, "in": True, "out": False}[active], order
    )
    pages = max(1, (total + 9) // 10)
    lines = [f"📋 گروه‌ها · صفحه {page + 1} از {pages} · مجموع {total}"]
    buttons = []
    for row in rows:
        lines.append(
            f"\n{(row.title or 'بدون عنوان')[:90]}\n{row.chat_id} · {'فعال' if row.is_active else 'خارج‌شده' if row.is_active is False else 'نامعلوم'} · ادمین {'بله' if row.is_admin else 'خیر' if row.is_admin is False else 'نامعلوم'}\nاعضا: {row.member_count if row.member_count is not None else 'نامعلوم'}"
        )
        buttons.append(
            [button(event.sender_id, (row.title or str(row.chat_id))[:45], "group", row.chat_id)]
        )
    buttons.append(
        [
            button(event.sender_id, label, "dir", f"0_{flt}_{order}")
            for flt, label in (("all", "همه"), ("in", "داخل"), ("out", "خارج"))
        ]
    )
    buttons.append(
        [
            button(event.sender_id, label, "dir", f"0_{active}_{sort}")
            for sort, label in (
                ("date", "تاریخ"),
                ("members", "اعضای بیشتر"),
                ("members_asc", "اعضای کمتر"),
                ("activity", "فعالیت"),
            )
        ]
    )
    nav = []
    if page:
        nav.append(button(event.sender_id, "قبلی", "dir", f"{page - 1}_{active}_{order}"))
    if page + 1 < pages:
        nav.append(button(event.sender_id, "بعدی", "dir", f"{page + 1}_{active}_{order}"))
    if nav:
        buttons.append(nav)
    from bot.handlers.admin import show

    await show(event, "\n".join(lines), buttons)


async def schedule_panel(event, admin, conversations, state):
    name = {
        CS.ADMIN_PROMO_PANEL: "promo",
        CS.ADMIN_NUDGE_PANEL: "admin_nudge",
        CS.ADMIN_WEEKLY_REPORT_PANEL: "weekly_report",
    }[state]
    config = (await admin.channels.get(event.sender_id))[name]
    await conversations.set_state(event.sender_id, event.chat_id, state)
    text = f"{'فعال' if config['enabled'] else 'غیرفعال'}" + (
        f" · هر {config['hours']} ساعت" if "hours" in config else " · گزارش هفتگی"
    )
    if name == "promo":
        text += "\n" + config["text"]
    markup = (
        keys.keyboard([[b.DISABLE if config["enabled"] else b.ENABLE], [b.BACK]])
        if name == "weekly_report"
        else keys.schedule(config["enabled"], text=name == "promo")
    )
    await event.respond(text, buttons=markup, parse_mode=None)


async def handle_message(event, admin, conversations, presentation=None):
    from bot.handlers.admin import private

    if not private(event) or conversations is None:
        return
    raw = (getattr(event, "raw_text", "") or "").strip()
    state, data = await conversations.get(event.sender_id, event.chat_id)
    if not state.value.startswith("ADMIN_"):
        return
    command = raw.split(maxsplit=1)[0].split("@", 1)[0] if raw else ""
    if command in {"/start", "/admin"}:
        await admin.clear_prompts(event.sender_id)
        await conversations.reset(event.sender_id, event.chat_id)
        return
    handled = True
    try:
        await admin.authorize(event.sender_id)
        if raw == b.CANCEL:
            await admin.clear_prompts(event.sender_id)
            await conversations.reset(event.sender_id, event.chat_id)
            await event.respond(
                "عملیات لغو شد؛ لینک رسانه را بفرست.",
                buttons=types.ReplyKeyboardHide(),
                parse_mode=None,
            )
        elif raw == b.BACK:
            await admin.clear_prompts(event.sender_id)
            parent = (
                CS(data.get("parent", CS.ADMIN_PANEL.value))
                if state is CS.ADMIN_INPUT
                else PARENTS.get(state, CS.ADMIN_PANEL)
            )
            await panel(event, admin, conversations, parent, presentation)
        elif state is CS.ADMIN_INPUT:
            await handle_input(event, admin, conversations, data, presentation)
        elif raw in CATEGORIES and state is CS.ADMIN_PANEL:
            await panel(event, admin, conversations, CATEGORIES[raw], presentation)
        elif raw == b.SETTINGS and state is CS.ADMIN_PANEL:
            await invoke(event, admin, "settings", presentation=presentation)
        elif raw in SUBMENUS and state is CS.ADMIN_CAT_CHANNELS_PANEL:
            await panel(event, admin, conversations, SUBMENUS[raw], presentation)
        elif state in {CS.ADMIN_PROMO_PANEL, CS.ADMIN_NUDGE_PANEL, CS.ADMIN_WEEKLY_REPORT_PANEL}:
            await handle_schedule(event, admin, conversations, state, raw)
        else:
            handled = await menu_action(event, admin, conversations, state, raw, presentation)
    except DownloadError as error:
        await event.respond(f"⚠️ {error}", parse_mode=None)
    except (ValueError, KeyError, TypeError, IndexError):
        await event.respond(
            "⚠️ ورودی معتبر نیست؛ دوباره امتحان کن یا بازگشت را بزن.",
            buttons=keys.BACK_MENU,
            parse_mode=None,
        )
    finally:
        if handled:
            raise events.StopPropagation


async def menu_action(event, admin, conversations, state, raw, presentation):
    if state is CS.ADMIN_CAT_ADMINS_PANEL:
        if raw in {b.ADD_ADMIN, b.REMOVE_ADMIN, b.DEVELOPER}:
            await ask(
                event,
                admin,
                conversations,
                {b.ADD_ADMIN: "grant", b.REMOVE_ADMIN: "revoke", b.DEVELOPER: "developer_id"}[raw],
                state,
            )
        elif raw == b.LIST_ADMINS:
            rows = await admin.administrators(event.sender_id)
            await event.respond(
                "🛡 مدیر اصلی: " + str(admin.main_admin_id or "تنظیم نشده"), parse_mode=None
            )
            for start in range(0, len(rows), 30):
                await event.respond(
                    "\n".join(
                        f"{row.user_id} · {(row.first_name or row.username or 'مدیر')[:70]}"
                        for row in rows[start : start + 30]
                    ),
                    parse_mode=None,
                )
        else:
            return False
    elif state is CS.ADMIN_CAT_MSG_PANEL:
        forms = {
            b.BROADCAST: ("broadcast", "users"),
            b.FORWARD: ("forward", "users"),
            b.GROUP_BROADCAST: ("broadcast", "groups"),
            b.GROUP_FORWARD: ("forward", "groups"),
        }
        targeted = {
            b.DM_USER: ("broadcast", "users"),
            b.DM_GROUP: ("broadcast", "groups"),
            b.FWD_USER: ("forward", "users"),
            b.FWD_GROUP: ("forward", "groups"),
        }
        if raw in forms:
            mode, audience = forms[raw]
            await ask(
                event, admin, conversations, "exclusions", state, mode=mode, audience=audience
            )
        elif raw in targeted:
            mode, audience = targeted[raw]
            await ask(event, admin, conversations, "target", state, mode=mode, audience=audience)
        elif raw == b.JOBS:
            await invoke(event, admin, "jobs", presentation=presentation)
        else:
            return False
    elif state is CS.ADMIN_GROUPS_PANEL:
        if raw == b.GROUP_LIST:
            await directory(event, admin)
        elif raw == b.GROUP_HEALTH:
            await invoke(event, admin, "health", presentation=presentation)
        elif raw == b.GROUP_IMPORT:
            token = await admin.prepare(event.sender_id, "crawl", {})
            await event.respond(
                "اعضای قابل مشاهدهٔ همهٔ گروه‌ها مرحله‌ای وارد شوند؟ محدودیت‌های خود تلگرام حفظ می‌شوند.",
                buttons=confirmation(event.sender_id, token),
                parse_mode=None,
            )
        elif raw in {b.GROUP_INFO, b.GROUP_MEMBERS, b.GROUP_LEAVE, b.CRAWL_STATUS}:
            await ask(
                event,
                admin,
                conversations,
                {
                    b.GROUP_INFO: "group_lookup",
                    b.GROUP_MEMBERS: "members",
                    b.GROUP_LEAVE: "leave",
                    b.CRAWL_STATUS: "crawl_status",
                }[raw],
                state,
            )
        elif raw in {b.NUDGE_ADMIN, b.NUDGE_OWNERS}:
            if raw == b.NUDGE_OWNERS:
                await admin.authorize(event.sender_id, root=True)
                await event.respond(
                    "یادآوریِ افزودن دوبارهٔ ربات برای چه کسانی فرستاده شود؟ فقط مخاطبان دارای استارت خصوصی می‌توانند پیام بگیرند.",
                    buttons=[
                        [button(event.sender_id, label, "nudge_target", target)]
                        for target, label in (
                            ("owner", "فقط مالک"),
                            ("admins", "فقط مدیران"),
                            ("both", "مالک و مدیران"),
                        )
                    ],
                    parse_mode=None,
                )
                return True
            kind = "nudge_admin" if raw == b.NUDGE_ADMIN else "nudge_owners"
            token = await admin.prepare(event.sender_id, kind, {})
            await event.respond(
                "ارسال یادآوری مرحله‌ای انجام شود؟",
                buttons=confirmation(event.sender_id, token),
                parse_mode=None,
            )
        else:
            return False
    elif state is CS.ADMIN_CAT_DATA_PANEL:
        reads = {
            b.STATS: "stats",
            b.DAILY: "daily",
            b.HISTORY: "daily",
            b.USERS: "users",
            b.CACHE: "cache",
            b.EXPORT_USERS: "exportu",
            b.EXPORT_GROUPS: "exportg",
        }
        if raw in reads:
            await invoke(event, admin, reads[raw], presentation=presentation)
        elif raw in {b.LOOKUP, b.TRACE, b.IMPORT_USERS, b.IMPORT}:
            await ask(
                event,
                admin,
                conversations,
                {
                    b.LOOKUP: "lookup",
                    b.TRACE: "trace",
                    b.IMPORT_USERS: "import_users",
                    b.IMPORT: "restore",
                }[raw],
                state,
            )
        elif raw == b.BACKUP:
            result = await admin.maintenance.backup(event.sender_id)
            stream = io.BytesIO(result.data)
            stream.name = result.filename
            await event.respond(
                "بک‌آپ داده‌های عمومی همین ربات؛ بدون نشست، کوکی، کلید فایل یا عملیات در انتظار. بازیابی فقط داده‌های مفقود را اضافه می‌کند.",
                file=stream,
                parse_mode=None,
            )
        elif raw == b.REFRESH_USERS:
            token = await admin.prepare(event.sender_id, "refresh_users", {})
            await event.respond(
                "نام و نام کاربری کاربران مرحله‌ای تازه شود؟",
                buttons=confirmation(event.sender_id, token),
                parse_mode=None,
            )
        else:
            return False
    elif state is CS.ADMIN_CAT_CLEANUP_PANEL:
        if raw in {b.FILTER_CLEANUP, b.PURGE_USERS}:
            from bot.handlers.admin_maintenance import open_cleanup

            await open_cleanup(event, admin, deleted_only=raw == b.PURGE_USERS)
        else:
            return False
    elif state in {
        CS.ADMIN_JOIN_PANEL,
        CS.ADMIN_ARCHIVE_PANEL,
        CS.ADMIN_STORAGE_PANEL,
        CS.ADMIN_WATCH_PANEL,
        CS.ADMIN_CAT_CHANNELS_PANEL,
    }:
        return await channel_action(event, admin, conversations, state, raw)
    else:
        return False
    return True


async def handle_input(event, admin, conversations, data, presentation):
    action, owner, raw = data["action"], event.sender_id, (event.raw_text or "").strip()
    parent = CS(data["parent"])
    if action in {"lookup", "trace"}:
        user = await admin.user(owner, int(raw) if raw.isdigit() else raw)
        if not user:
            raise DownloadError("کاربر پیدا نشد.")
        await invoke(
            event, admin, "trace" if action == "trace" else "user", user.user_id, presentation
        )
        return
    if action in {"group_lookup", "members", "leave", "crawl_status"}:
        identity = int(raw)
        if identity >= 0:
            raise DownloadError("شناسه منفی گروه را بفرست.")
        if action == "crawl_status":
            progress = await admin.crawl_progress(owner, identity)
            await event.respond(
                "🕷 وضعیت خزش\n"
                + (
                    "\n".join(f"{key}: {value}" for key, value in progress.items())
                    if progress
                    else "برای این گروه هنوز خزشی ثبت نشده است."
                ),
                parse_mode=None,
            )
        elif action == "members":
            await member_directory(event, admin, identity)
        else:
            await invoke(
                event,
                admin,
                "group" if action == "group_lookup" else "leave",
                identity,
                presentation,
            )
        return
    if action in {"grant", "revoke", "ban", "unban", "limit"}:
        values = raw.split()
        await admin.mutate(
            owner, action, int(values[0]), int(values[1]) if action == "limit" else None
        )
    elif action == "developer_id":
        identity = int(raw)
        await admin.channels.update(owner, "developer_id", identity or None)
    elif action in {"exclusions", "exclude_adders", "target"}:
        if action == "target":
            identity = int(raw)
            if (
                not -(2**63) < identity < 2**63
                or identity == 0
                or (identity > 0) != (data["audience"] == "users")
            ):
                raise DownloadError("شناسه مقصد با نوع مخاطب یکسان نیست.")
            extra = {"target_id": identity}
        elif action == "exclusions":
            extra = {"exclude": parse_ids(raw, positive=data["audience"] == "users")}
            if data["audience"] == "groups":
                await ask(
                    event,
                    admin,
                    conversations,
                    "exclude_adders",
                    parent,
                    **{
                        key: value for key, value in data.items() if key not in {"action", "parent"}
                    },
                    **extra,
                )
                return
        else:
            extra = {"exclude_adders": parse_ids(raw, positive=True)}
        payload = {key: value for key, value in data.items() if key not in {"action", "parent"}}
        await ask(event, admin, conversations, data["mode"], parent, **(payload | extra))
        return
    elif action in {"broadcast", "forward"}:
        payload = {
            key: value for key, value in data.items() if key not in {"action", "parent", "mode"}
        }
        if action == "broadcast" and not getattr(event, "media", None):
            payload["text"] = raw
        else:
            payload["source_id"] = event.id
            if action == "broadcast":
                payload["copy"] = True
        token = await admin.prepare(owner, action, payload)
        await conversations.set_state(owner, event.chat_id, parent)
        await event.respond(
            f"📨 مخاطبان: {payload['audience']}\nاستثنا: {len(payload.get('exclude', []))} مقصد، {len(payload.get('exclude_adders', []))} آورنده\nشروع ارسال را تأیید می‌کنی؟",
            buttons=confirmation(owner, token),
            parse_mode=None,
        )
        return
    elif action in {"import_users", "restore"}:
        cap = 1024 * 1024 if action == "import_users" else 20 * 1024 * 1024
        size = getattr(getattr(event, "file", None), "size", None)
        if type(size) is not int or not 0 < size <= cap:
            raise DownloadError(
                f"فایل با اندازه مشخص و حداکثر {cap // 1024 // 1024} مگابایت بفرست."
            )
        content = await event.download_media(bytes)
        if action == "restore":
            token, counts = await admin.maintenance.prepare_restore(owner, content)
            await event.respond(
                "داده‌ها بررسی شدند؛ فقط رکوردهای مفقود افزوده می‌شوند.\n"
                + "\n".join(f"{key}: {value}" for key, value in counts.items()),
                buttons=[[button(owner, "✅ تأیید بازیابی", "restore", token)]],
                parse_mode=None,
            )
        else:
            count = await admin.import_users(owner, content)
            await event.respond(f"✅ {count} ردیف معتبر وارد شد.", parse_mode=None)
    elif action == "toggles":
        name = data["key"]
        labels = b.POINTS if name == "join_points" else b.KINDS
        if raw in {b.ALL_ON, b.ALL_OFF}:
            enabled = await admin.channels.modify(
                owner, name, lambda previous: list(labels) if raw == b.ALL_ON else []
            )
        else:
            key = next(
                (key for key, label in labels.items() if raw in {f"✅ {label}", f"☐ {label}"}), None
            )
            if key is None:
                raise DownloadError("یکی از دکمه‌های همین بخش را انتخاب کن.")
            enabled = await admin.channels.modify(
                owner, name, lambda previous: sorted(set(previous).symmetric_difference({key}))
            )
        await event.respond(
            "✅ تنظیم ذخیره شد.", buttons=keys.toggles(labels, enabled), parse_mode=None
        )
        return
    elif action in {
        "channel_add",
        "channel_remove",
        "archive",
        "storage",
        "watch_group",
        "watch_channel",
        "promo_text",
    }:
        await channel_input(event, admin, conversations, data)
        return
    else:
        raise DownloadError("فرم مدیریت معتبر نیست.")
    await panel(event, admin, conversations, parent, presentation)


async def member_directory(event, admin, chat, page=0, active="all", started="all"):
    rows, total, page = await admin.member_directory(event.sender_id, chat, page, active, started)
    pages = max(1, (total + 9) // 10)
    text = (
        f"👥 اعضای گروه {chat} · {total} نفر\nصفحه {page + 1} از {pages}\nفقط اعضای قابل مشاهدهٔ تلگرام و کاربران مشاهده‌شده ثبت می‌شوند.\n"
        + "\n".join(
            f"{row['user_id']} · {(row['first_name'] or row['username'] or 'کاربر')[:55]} · {'داخل' if row['is_active'] else 'خارج'} · {'استارت زده' if row['is_started'] else 'بدون استارت'} · {row['message_count']} پیام"
            for row in rows
        )
    )
    prefix = compact_id(chat)
    buttons = [
        [
            button(event.sender_id, label, "mdir", f"{prefix}_0_{value}_{started}")
            for value, label in (("all", "همه اعضا"), ("in", "داخل"), ("out", "خارج"))
        ],
        [
            button(event.sender_id, label, "mdir", f"{prefix}_0_{active}_{value}")
            for value, label in (("all", "همه"), ("yes", "استارت زده"), ("no", "بدون استارت"))
        ],
    ]
    nav = []
    if page:
        nav.append(
            button(event.sender_id, "قبلی", "mdir", f"{prefix}_{page - 1}_{active}_{started}")
        )
    if page + 1 < pages:
        nav.append(
            button(event.sender_id, "بعدی", "mdir", f"{prefix}_{page + 1}_{active}_{started}")
        )
    if nav:
        buttons.append(nav)
    buttons.append([button(event.sender_id, "🕷 خزش همین گروه", "crawl", chat)])
    from bot.handlers.admin import show

    await show(event, text, buttons)


async def channel_action(event, admin, conversations, state, raw):
    config = await admin.channels.get(event.sender_id)
    prompts = {
        b.ADD_CHANNEL: "channel_add",
        b.REMOVE_CHANNEL: "channel_remove",
        b.SET_ARCHIVE: "archive",
        b.SET_STORAGE: "storage",
        b.WATCH_ADD: "watch_group",
    }
    if raw in prompts:
        await ask(event, admin, conversations, prompts[raw], state)
    elif raw == b.WATCH_LIST:
        await watch_page(event, admin)
    elif raw in {b.LIST_CHANNELS, b.VIEW_ARCHIVE, b.VIEW_STORAGE}:
        name = {
            b.LIST_CHANNELS: "forced_channels",
            b.VIEW_ARCHIVE: "archive",
            b.VIEW_STORAGE: "storage",
        }[raw]
        values = config[name]
        rows = values if isinstance(values, list) else [values] if values else []
        if not rows:
            await event.respond("موردی تنظیم نشده است.", parse_mode=None)
        for start in range(0, len(rows), 5):
            await event.respond(
                "\n".join(
                    f"{row['id']} · {row['title'][:90]}\n{row['link']}"
                    for row in rows[start : start + 5]
                ),
                parse_mode=None,
            )
    elif raw in {b.REMOVE_ARCHIVE, b.REMOVE_STORAGE}:
        await admin.channels.update(
            event.sender_id, "archive" if raw == b.REMOVE_ARCHIVE else "storage", None
        )
        await event.respond("✅ کانال حذف شد.", parse_mode=None)
    elif raw in {b.STORAGE_KINDS, b.JOIN_POINTS}:
        name = "storage_kinds" if raw == b.STORAGE_KINDS else "join_points"
        await conversations.set_state(
            event.sender_id,
            event.chat_id,
            CS.ADMIN_INPUT,
            action="toggles",
            parent=state.value,
            key=name,
        )
        await event.respond(
            "محل‌های بررسی را انتخاب کن."
            if name == "join_points"
            else "نوع پیام‌های قابل ذخیره را انتخاب کن.",
            buttons=keys.toggles(b.POINTS if name == "join_points" else b.KINDS, config[name]),
            parse_mode=None,
        )
    elif raw in {b.PROMO, b.AUTO_NUDGE, b.WEEKLY}:
        await schedule_panel(
            event,
            admin,
            conversations,
            {
                b.PROMO: CS.ADMIN_PROMO_PANEL,
                b.AUTO_NUDGE: CS.ADMIN_NUDGE_PANEL,
                b.WEEKLY: CS.ADMIN_WEEKLY_REPORT_PANEL,
            }[raw],
        )
    else:
        return False
    return True


async def channel_input(event, admin, conversations, data):
    action, owner = data["action"], event.sender_id
    config = await admin.channels.get(owner)
    if action == "watch_group":
        chat = int(event.raw_text)
        if chat >= 0 or await admin.group(owner, chat) is None:
            raise DownloadError("شناسه یک گروه ثبت‌شده را بفرست.")
        await ask(event, admin, conversations, "watch_channel", CS(data["parent"]), chat_id=chat)
        return
    if action == "channel_remove":
        identity = int(event.raw_text)
        await admin.channels.remove_channel(owner, identity)
    elif action == "promo_text":
        await admin.channels.modify(
            owner, "promo", lambda previous: {**previous, "text": event.raw_text}
        )
        await schedule_panel(event, admin, conversations, CS.ADMIN_PROMO_PANEL)
        return
    else:
        channel = await parse_channel(event)
        if action == "channel_add":
            await admin.channels.upsert_channel(owner, "forced_channels", channel)
        elif action == "watch_channel":
            await admin.channels.upsert_watch(
                owner,
                {"chat_id": data["chat_id"], "channel": channel, "kinds": config["storage_kinds"]},
            )
        else:
            await admin.channels.update(owner, action, channel)
    await event.respond("✅ تنظیم ذخیره شد.", parse_mode=None)
    await panel(event, admin, conversations, CS(data["parent"]))


async def handle_schedule(event, admin, conversations, state, raw):
    name = {
        CS.ADMIN_PROMO_PANEL: "promo",
        CS.ADMIN_NUDGE_PANEL: "admin_nudge",
        CS.ADMIN_WEEKLY_REPORT_PANEL: "weekly_report",
    }[state]
    if raw == b.SET_TEXT and name == "promo":
        await ask(event, admin, conversations, "promo_text", state)
        return
    if raw in {b.ENABLE, b.DISABLE}:
        field, value = "enabled", raw == b.ENABLE
    elif raw.isdigit() and name != "weekly_report" and 1 <= int(raw) <= 24:
        field, value = "hours", int(raw)
    else:
        raise DownloadError("از دکمه‌های همین بخش انتخاب کن.")
    await admin.channels.modify(event.sender_id, name, lambda previous: {**previous, field: value})
    await schedule_panel(event, admin, conversations, state)


async def watch_page(event, admin, page=0):
    rows = (await admin.channels.get(event.sender_id))["watches"]
    page = min(max(0, page), max(0, (len(rows) - 1) // 8))
    subset = rows[page * 8 : page * 8 + 8]
    buttons = [
        [
            button(
                event.sender_id,
                f"{row['chat_id']} · {row['channel']['title'][:40]}",
                "watch",
                row["chat_id"],
            )
        ]
        for row in subset
    ]
    nav = []
    if page:
        nav.append(button(event.sender_id, "قبلی", "watchpage", page - 1))
    if (page + 1) * 8 < len(rows):
        nav.append(button(event.sender_id, "بعدی", "watchpage", page + 1))
    if nav:
        buttons.append(nav)
    from bot.handlers.admin import show

    await show(event, f"👁️ رصدها · {len(rows)} مورد · صفحه {page + 1}", buttons)


async def watch_detail(event, admin, identity):
    rows = (await admin.channels.get(event.sender_id))["watches"]
    row = next((row for row in rows if row["chat_id"] == identity), None)
    if row is None:
        raise DownloadError("رصد پیدا نشد.")
    buttons = [
        [
            button(
                event.sender_id,
                f"{'✅' if key in row['kinds'] else '☐'} {label}",
                "wkind",
                f"{compact_id(identity)}_{key}",
            )
        ]
        for key, label in b.KINDS.items()
    ]
    buttons.append(
        [
            button(event.sender_id, b.ALL_ON, "wkind", f"{compact_id(identity)}_all"),
            button(event.sender_id, b.ALL_OFF, "wkind", f"{compact_id(identity)}_none"),
        ]
    )
    buttons.append(
        [
            button(event.sender_id, "حذف رصد", "watchdel", identity),
            button(event.sender_id, b.BACK, "watchpage"),
        ]
    )
    from bot.handlers.admin import show

    await show(
        event, f"👁️ گروه {identity}\nمقصد: {row['channel']['id']}\n{row['channel']['link']}", buttons
    )
