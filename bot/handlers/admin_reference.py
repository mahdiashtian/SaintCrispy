"""State-routed private administration, adapted from the local Couplyo panel."""

import io
import re
from types import SimpleNamespace

from telethon import events, types, utils

from bot.handlers.admin_render import source_reply
from bot.keyboards import admin_reply as keys
from bot.keyboards.admin import button, compact_id, confirmation
from bot.texts import admin_buttons as b
from bot.texts import couplyo_admin as copy
from bot.texts.admin import PROMPTS
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
        copy.admin_panel(getattr(getattr(event, "sender", None), "first_name", None))
        if state is CS.ADMIN_PANEL
        else {
            CS.ADMIN_CAT_ADMINS_PANEL: copy.ADMIN_CAT_ADMINS_PANEL,
            CS.ADMIN_CAT_MSG_PANEL: copy.ADMIN_CAT_MSG_PANEL,
            CS.ADMIN_GROUPS_PANEL: copy.ADMIN_GROUPS_PANEL,
            CS.ADMIN_CAT_CHANNELS_PANEL: copy.ADMIN_CAT_CHANNELS_PANEL,
            CS.ADMIN_CAT_DATA_PANEL: copy.ADMIN_CAT_DATA_PANEL,
            CS.ADMIN_JOIN_PANEL: copy.ADMIN_JOIN_PANEL,
            CS.ADMIN_ARCHIVE_PANEL: copy.ADMIN_ARCHIVE_PANEL,
            CS.ADMIN_STORAGE_PANEL: copy.ADMIN_STORAGE_PANEL,
            CS.ADMIN_WATCH_PANEL: copy.ADMIN_WATCH_PANEL,
            CS.ADMIN_CAT_CLEANUP_PANEL: "<b>🧹 پاکسازی دیتابیس</b> ⚠️\nهمه‌ی حذف‌های خطرناک اینجان. فقط ادمینِ اصلی بهشون دسترسی داره و هیچ‌کدوم برنمی‌گرده.\n\nقبل از هر حذفی دقیقاً بهت می‌گم چی حذف می‌شه، چی دست‌نخورده می‌مونه و چند مورد شامل می‌شه، بعد ازت تأیید می‌گیرم. تا تأیید نکنی هیچی حذف نمی‌شه ✅\n\nقبل از هر کاری بهتره بک‌آپ بگیری 💾",
        }.get(state, copy.CHOOSE_FROM_LIST)
    )
    await source_reply(event, title, buttons=keys.menu(state), presentation=presentation)


async def ask(event, admin, conversations, action, parent, **data):
    await admin.authorize(
        event.sender_id,
        root=action not in {"lookup", "trace", "group_lookup", "members", "crawl_status"},
    )
    await conversations.set_state(
        event.sender_id, event.chat_id, CS.ADMIN_INPUT, action=action, parent=parent.value, **data
    )
    text = (
        {
            "grant": copy.ADMIN_ADD_PROMPT,
            "revoke": copy.ADMIN_REMOVE_PROMPT,
            "lookup": copy.ADMIN_USER_LOOKUP_PROMPT,
            "group_lookup": copy.ADMIN_GROUPS_INFO_PROMPT,
            "members": copy.ADMIN_GROUPS_MEMBERS_PROMPT,
            "channel_add": copy.CHANNEL_ADD_PROMPT,
            "channel_remove": copy.CHANNEL_REMOVE_PROMPT,
            "archive": copy.ARCHIVE_SET_PROMPT,
            "storage": copy.STORAGE_SET_PROMPT,
            "watch_group": copy.WATCH_ADD_GROUP_PROMPT,
            "watch_channel": copy.WATCH_ADD_CHANNEL_PROMPT,
            "exclusions": copy.ADMIN_GROUP_EXCLUDE_CHATS_PROMPT,
            "exclude_adders": copy.ADMIN_GROUP_EXCLUDE_USERS_PROMPT,
            "forward": copy.ADMIN_FORWARD_PROMPT,
            "broadcast": copy.ADMIN_BROADCAST_PROMPT,
        }.get(action)
        or PROMPTS.get(action)
        or {
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
    )
    if action == "trace":
        text = "آیدیِ عددی یا یوزرنیمِ کاربر رو بفرست (یا روی پیامش ریپلای کن) تا کلِ ردِ پاش رو بدم: گروه‌هاش، پروفایلش و فعالیتش 🕵️"
    if action == "developer_id":
        text = copy.admin_developer_prompt(
            str((await admin.channels.get(event.sender_id))["developer_id"] or "") or None
        )
    if action == "target":
        text = (
            copy.ADMIN_TARGET_USER_ID_PROMPT
            if data.get("audience") == "users"
            else copy.ADMIN_TARGET_GROUP_ID_PROMPT
        )
    if action in {"broadcast", "forward"} and data.get("target_id"):
        text = (
            copy.ADMIN_TARGET_SEND_PROMPT if action == "broadcast" else copy.ADMIN_TARGET_FWD_PROMPT
        )
    elif action in {"broadcast", "forward"} and data.get("audience") == "groups":
        text = copy.admin_group_broadcast_prompt(
            "copy" if action == "broadcast" else "forward",
            data.get("group_audience"),
            chat_count=len(data.get("exclude", [])),
            user_count=len(data.get("exclude_members", [])),
        )
    await source_reply(
        event,
        text,
        buttons=keys.keyboard([[b.DEV_CLEAR], [b.BACK]])
        if action == "developer_id"
        else keys.EXCLUSIONS
        if action in {"exclusions", "exclude_adders"}
        else keys.BACK_MENU,
    )


async def invoke(event, admin, action, value="0", presentation=None):
    from bot.handlers.admin import callback

    await callback(Invocation(event, action, value), admin, presentation)


def parse_ids(raw, *, positive=None):
    if raw == b.SKIP:
        return []
    normalized = raw.translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    ).translate(dict.fromkeys((0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))))
    values = [line.strip() for line in normalized.splitlines() if line.strip()]
    invalid = (
        copy.ADMIN_GROUP_EXCLUDE_USERS_INVALID
        if positive
        else copy.ADMIN_GROUP_EXCLUDE_CHATS_INVALID
    )
    if not values or any(re.fullmatch(r"-?[0-9]{1,19}", value) is None for value in values):
        raise DownloadError(invalid)
    if len(values) > 1000:
        raise DownloadError("حداکثر هزار استثنا وارد کن.")
    try:
        result = list(dict.fromkeys(int(value) for value in values))
    except ValueError as error:
        raise DownloadError(invalid) from error
    if any(
        value == 0
        or not -(2**63) < value < 2**63
        or (positive is True and value < 0)
        or (positive is False and value > 0)
        for value in result
    ):
        raise DownloadError(invalid)
    return sorted(result)


async def parse_channel(event):
    pieces = (event.raw_text or "").strip().split()
    if not pieces:
        raise DownloadError("کانال را مشخص کن.")
    query = (
        int(pieces[0])
        if pieces[0].lstrip("-").isdigit()
        else int(pieces[1])
        if len(pieces) > 1 and pieces[1].lstrip("-").isdigit()
        else pieces[0]
    )
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
        else (
            pieces[0]
            if pieces[0].startswith("https://t.me/")
            else pieces[1]
            if len(pieces) > 1
            else ""
        )
    )
    if not link.startswith("https://t.me/"):
        raise DownloadError("برای کانال خصوصی، شناسه عددی و لینک دعوت را با فاصله بفرست.")
    return {"id": utils.get_peer_id(entity), "link": link, "title": (entity.title or "")[:256]}


async def directory(event, admin, page=0, active="all", order="date", *, edit=False):
    rows, total, page = await admin.group_directory(
        event.sender_id, page, {"all": None, "in": True, "out": False}[active], order
    )
    pages = max(1, (total + 9) // 10)
    text = copy.groups_list_page(
        [copy.group_card(row) for row in rows],
        page,
        pages,
        total,
        active,
        {"members": "mem", "members_asc": "mem_asc"}.get(order, order),
    )
    buttons = []
    for row in rows:
        buttons.append(
            [button(event.sender_id, (row.title or str(row.chat_id))[:45], "group", row.chat_id)]
        )
    buttons.append(
        [
            button(
                event.sender_id, ("✅ " if flt == active else "") + label, "dir", f"0_{flt}_{order}"
            )
            for flt, label in (("all", "همه"), ("in", "عضو"), ("out", "خارج‌شده"))
        ]
    )
    buttons.append(
        [
            button(
                event.sender_id,
                ("✅ " if sort == order else "") + label,
                "dir",
                f"0_{active}_{sort}",
            )
            for sort, label in (
                ("date", "جدیدترین"),
                ("members", "بیشترین عضو"),
                ("members_asc", "کمترین عضو"),
                ("activity", "فعالیت"),
            )
        ]
    )
    nav = []
    if page:
        nav.append(button(event.sender_id, "◀️ قبلی", "dir", f"{page - 1}_{active}_{order}"))
    if page + 1 < pages:
        nav.append(button(event.sender_id, "بعدی ▶️", "dir", f"{page + 1}_{active}_{order}"))
    if nav:
        buttons.append(nav)
    buttons.append([button(event.sender_id, "🔄 بروزرسانی (فچِ دوباره)", "health")])
    await source_reply(event, text, buttons=buttons, edit=getattr(event, "data", None) is not None)


async def schedule_panel(event, admin, conversations, state):
    name = {
        CS.ADMIN_PROMO_PANEL: "promo",
        CS.ADMIN_NUDGE_PANEL: "admin_nudge",
        CS.ADMIN_WEEKLY_REPORT_PANEL: "weekly_report",
    }[state]
    config = (await admin.channels.get(event.sender_id))[name]
    await conversations.set_state(event.sender_id, event.chat_id, state)
    if name == "promo":
        text = copy.promo_panel(interval_hours=config["hours"], enabled=config["enabled"])
    elif name == "admin_nudge":
        text = copy.admin_nudge_panel(interval_hours=config["hours"], enabled=config["enabled"])
    else:
        status = "فعاله 🟢" if config["enabled"] else "خاموشه 🔴"
        text = f"<b>گزارش‌های روزانه و هفتگی تصویری</b> 📊\n\nوضعیت: <b>{status}</b>\nگزارش روزانه: <b>هر شب ساعت ۰۰:۰۰</b> به وقت تهران\nگزارش هفتگی: <b>شنبه‌ها، ساعت ۱۲</b> به وقت تهران\n\nآمارِ پیام‌ها و اعضای قابل‌مشاهدهٔ ربات گزارش می‌شود.\nادمین‌های هر گروه روزی یک‌بار می‌تونن با <code>/weeklyreport</code> گزارش شنبه تا الان و با <code>/dailyreport</code> گزارش امروز رو بگیرن؛ دو لیمیت از هم جدا هستن و ادمین اصلی ربات محدودیت نداره."
    markup = (
        keys.keyboard([[b.DISABLE if config["enabled"] else b.ENABLE], [b.BACK]])
        if name == "weekly_report"
        else keys.schedule(config["enabled"], text=name == "promo")
    )
    await source_reply(event, text, buttons=markup)


async def handle_message(event, admin, conversations, presentation=None):
    from bot.handlers.admin import private

    if not private(event) or conversations is None:
        return
    raw = (getattr(event, "raw_text", "") or "").strip()
    raw = {"انصراف": b.CANCEL, "بازگشت": b.BACK}.get(raw, raw)
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
            if state is CS.ADMIN_INPUT and data.get("action") == "exclude_adders":
                await ask(
                    event,
                    admin,
                    conversations,
                    "exclusions",
                    CS(data["parent"]),
                    **{
                        key: value for key, value in data.items() if key not in {"action", "parent"}
                    },
                )
                return
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
            lines = (
                [f"👑 <code>{copy.pd(admin.main_admin_id)}</code> (ادمین اصلی)"]
                if admin.main_admin_id
                else []
            )
            import html

            lines.extend(
                f"👤 <a href='tg://user?id={row.user_id}'>{html.escape(row.first_name or row.username or 'یه کاربر ناشناس')}</a> — <code>{copy.pd(row.user_id)}</code>"
                for row in rows
                if row.user_id != admin.main_admin_id
            )
            await source_reply(
                event,
                copy.ADMIN_LIST_HEADER + ("\n".join(lines) if lines else copy.ADMIN_LIST_EMPTY),
                buttons=keys.menu(state),
                presentation=presentation,
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
            if audience == "groups":
                await admin.authorize(event.sender_id, root=True)
                encoded = "b" if mode == "broadcast" else "f"
                await source_reply(
                    event,
                    copy.admin_group_audience_ask("copy" if mode == "broadcast" else "forward"),
                    buttons=[
                        [button(event.sender_id, label, "audience", f"{encoded}_{scope}")]
                        for scope, label in (
                            ("both", "👥 همه‌ی گروه‌های فعال"),
                            ("admin", "🛡 فقط جایی که ادمینیم"),
                            ("non_admin", "👤 فقط جایی که ادمین نیستیم"),
                        )
                    ]
                    + [[button(event.sender_id, "✖️ بی‌خیال", "home")]],
                )
            else:
                await ask(event, admin, conversations, mode, state, mode=mode, audience=audience)
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
                            ("owner", "👑 فقط مالکِ گروه"),
                            ("admins", "🛡 فقط مدیرا"),
                            ("both", "👥 هم مالک، هم مدیرا"),
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
            b.DAILY: "daily_summary",
            b.HISTORY: "stats_history",
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
        if raw == b.PURGE_USERS:
            token = await admin.prepare(event.sender_id, "purge_users", {})
            await source_reply(
                event,
                "کاربرها رو از تلگرام بررسی کنم و بات‌ها و اکانت‌های حذف‌شده رو پاک کنم؟\nکاربرهای محافظت‌شده و مواردی که قابل بررسی نباشن حفظ می‌شن.",
                buttons=confirmation(event.sender_id, token),
            )
        elif raw == b.FILTER_CLEANUP:
            from bot.handlers.admin_maintenance import open_cleanup

            await open_cleanup(event, admin)
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
        query = raw
        if getattr(event, "is_reply", False):
            replied = await event.get_reply_message()
            forwarded = getattr(replied, "fwd_from", None)
            peer = getattr(forwarded, "from_id", None)
            identity = (
                peer.user_id
                if isinstance(peer, types.PeerUser)
                else getattr(replied, "sender_id", None)
            )
            if type(identity) is int and identity > 0:
                query = identity
        user = await admin.user(
            owner, int(query) if isinstance(query, str) and query.isdigit() else query
        )
        if not user:
            raise DownloadError(copy.ADMIN_USER_LOOKUP_INVALID)
        result = await admin.public_trace(owner, user.user_id)
        await source_reply(
            event,
            copy.downloader_user_trace(result, with_history=action == "trace")
            + copy.ADMIN_LOOKUP_AGAIN_HINT,
            buttons=keys.BACK_MENU,
            presentation=presentation,
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
        if action in {"grant", "revoke"}:
            await conversations.set_state(owner, event.chat_id, parent)
            await source_reply(
                event,
                copy.ADMIN_ADDED if action == "grant" else copy.ADMIN_REMOVED,
                buttons=keys.menu(parent),
            )
            return
    elif action == "developer_id":
        if not raw:
            raise DownloadError(copy.ADMIN_DEV_INVALID)
        cleared = raw == b.DEV_CLEAR
        await admin.channels.update(owner, "developer_id", None if cleared else raw)
        await source_reply(
            event,
            copy.ADMIN_DEV_CLEARED if cleared else copy.admin_developer_set(raw),
            buttons=keys.menu(CS.ADMIN_CAT_ADMINS_PANEL),
        )
        await conversations.set_state(owner, event.chat_id, CS.ADMIN_CAT_ADMINS_PANEL)
        return
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
            extra = {"exclude_members": parse_ids(raw, positive=True)}
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
            copy.admin_group_audience_label(payload.get("group_audience"))
            + "\n"
            + copy.admin_group_exclusions_summary(
                len(payload.get("exclude", [])), len(payload.get("exclude_members", []))
            )
            if payload["audience"] == "groups"
            else "شروع ارسال را تأیید می‌کنی؟",
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
                (
                    key
                    for key, label in labels.items()
                    if raw in {f"✅ {label}", f"☐ {label}", f"⬜ {label}"}
                ),
                None,
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
    group = await admin.group(event.sender_id, chat)
    text = copy.group_members_page(
        [SimpleNamespace(**row) for row in rows],
        page,
        pages,
        total,
        chat,
        group.title if group else None,
        active=active,
        started=started,
    )
    prefix = compact_id(chat)
    buttons = [
        [
            button(
                event.sender_id,
                ("✅ " if value == active else "") + label,
                "mdir",
                f"{prefix}_0_{value}_{started}",
            )
            for value, label in (("all", "همه"), ("in", "عضو"), ("out", "خارج‌شده"))
        ],
        [
            button(
                event.sender_id,
                ("✅ " if value == started else "") + label,
                "mdir",
                f"{prefix}_0_{active}_{value}",
            )
            for value, label in (("all", "همه"), ("yes", "استارت‌زده"), ("no", "استارت‌نزده"))
        ],
    ]
    nav = []
    if page:
        nav.append(
            button(event.sender_id, "◀️ قبلی", "mdir", f"{prefix}_{page - 1}_{active}_{started}")
        )
    if page + 1 < pages:
        nav.append(
            button(event.sender_id, "بعدی ▶️", "mdir", f"{prefix}_{page + 1}_{active}_{started}")
        )
    if nav:
        buttons.append(nav)
    buttons.append([button(event.sender_id, "🕷 خزش همین گروه", "crawl", chat)])
    await source_reply(event, text, buttons=buttons, edit=getattr(event, "data", None) is not None)


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
        header, empty = {
            "forced_channels": (copy.CHANNEL_LIST_HEADER, copy.CHANNEL_LIST_EMPTY),
            "archive": (copy.ARCHIVE_HEADER, copy.ARCHIVE_NONE),
            "storage": (copy.STORAGE_HEADER, copy.STORAGE_NONE),
        }[name]
        await source_reply(
            event,
            header + "".join(copy.channel_list_item(row["link"], row["id"]) for row in rows)
            if rows
            else empty,
            buttons=keys.menu(state),
        )
    elif raw in {b.REMOVE_ARCHIVE, b.REMOVE_STORAGE}:
        name = "archive" if raw == b.REMOVE_ARCHIVE else "storage"
        if config[name]:
            await admin.channels.update(event.sender_id, name, None)
            text = copy.ARCHIVE_REMOVED if name == "archive" else copy.STORAGE_REMOVED
        else:
            text = copy.ARCHIVE_NONE if name == "archive" else copy.STORAGE_NONE
        await source_reply(event, text, buttons=keys.menu(state))
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
        text = (
            copy.join_points_panel(set(config[name]), has_channels=bool(config["forced_channels"]))
            if name == "join_points"
            else copy.storage_kinds_panel(set(config[name]), has_channel=bool(config["storage"]))
        )
        await source_reply(
            event,
            text,
            buttons=keys.toggles(b.POINTS if name == "join_points" else b.KINDS, config[name]),
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
        try:
            chat = int(event.raw_text)
        except ValueError as error:
            raise DownloadError(copy.WATCH_BAD_GROUP) from error
        if chat >= 0 or await admin.group(owner, chat) is None:
            raise DownloadError(copy.WATCH_BAD_GROUP)
        await ask(event, admin, conversations, "watch_channel", CS(data["parent"]), chat_id=chat)
        return
    if action == "channel_remove":
        raw = event.raw_text.strip().rstrip("/")
        found = next(
            (
                row
                for row in config["forced_channels"]
                if str(row["id"]) == raw or row["link"].rstrip("/") == raw
            ),
            None,
        )
        if found is None:
            raise DownloadError(copy.CHANNEL_NOT_FOUND)
        await admin.channels.remove_channel(owner, found["id"])
        text = copy.CHANNEL_REMOVED
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
            text = copy.CHANNEL_ADDED
        elif action == "watch_channel":
            await admin.channels.upsert_watch(
                owner, {"chat_id": data["chat_id"], "channel": channel, "kinds": []}
            )
            group = await admin.group(owner, data["chat_id"])
            text = copy.watch_added(
                data["chat_id"], group.title if group else None, channel["link"]
            )
        else:
            await admin.channels.update(owner, action, channel)
            text = copy.ARCHIVE_SET_DONE if action == "archive" else copy.STORAGE_SET_DONE
    await conversations.set_state(owner, event.chat_id, CS(data["parent"]))
    await source_reply(event, text, buttons=keys.menu(CS(data["parent"])))


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
    pages = max(1, (len(rows) + 7) // 8)
    page = min(max(0, page), pages - 1)
    subset = rows[page * 8 : page * 8 + 8]
    buttons, lines = [], []
    for row in subset:
        group = await admin.group(event.sender_id, row["chat_id"])
        title = group.title if group else None
        buttons.append(
            [
                button(
                    event.sender_id,
                    copy.watch_button_label(row["chat_id"], title),
                    "watch",
                    row["chat_id"],
                )
            ]
        )
        lines.append(copy.watch_list_row(row["chat_id"], title, len(row["kinds"])))
    nav = []
    if page:
        nav.append(button(event.sender_id, "◀️ قبلی", "watchpage", page - 1))
    if page + 1 < pages:
        nav.append(button(event.sender_id, "بعدی ▶️", "watchpage", page + 1))
    if nav:
        buttons.append(nav)
    await source_reply(
        event,
        copy.watch_list_header(len(rows), page, pages) + "\n\n" + "".join(lines)
        if rows
        else copy.WATCH_EMPTY,
        buttons=buttons,
        edit=getattr(event, "data", None) is not None,
    )


async def watch_detail(event, admin, identity, *, confirm=False):
    rows = (await admin.channels.get(event.sender_id))["watches"]
    row = next((row for row in rows if row["chat_id"] == identity), None)
    if row is None:
        raise DownloadError(copy.WATCH_GONE)
    group = await admin.group(event.sender_id, identity)
    title = group.title if group else None
    if confirm:
        text = copy.watch_delete_confirm(identity, title)
        buttons = [
            [
                button(event.sender_id, "✅ آره، حذف کن", "watchconfirm", identity),
                button(event.sender_id, "↩️ بی‌خیال", "watch", identity),
            ]
        ]
    else:
        kinds = [
            button(
                event.sender_id,
                f"{'✅' if key in row['kinds'] else '⬜'} {label}",
                "wkind",
                f"{compact_id(identity)}_{key}",
            )
            for key, label in b.KINDS.items()
        ]
        buttons = [kinds[index : index + 2] for index in range(0, len(kinds), 2)]
        buttons += [
            [
                button(event.sender_id, b.ALL_ON, "wkind", f"{compact_id(identity)}_all"),
                button(event.sender_id, b.ALL_OFF, "wkind", f"{compact_id(identity)}_none"),
            ],
            [
                button(event.sender_id, "🗑️ حذف رصد", "watchdel", identity),
                button(event.sender_id, "↩️ لیست رصدها", "watchpage"),
            ],
        ]
        text = copy.watch_detail(
            identity,
            title,
            row["channel"]["link"],
            row["channel"]["id"],
            row["kinds"],
            member_count=group.member_count if group else None,
        )
    await source_reply(event, text, buttons=buttons, edit=getattr(event, "data", None) is not None)
