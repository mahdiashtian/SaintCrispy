"""Durable channel forwarding, opt-in schedules and account-scoped reports."""

import asyncio
import base64
import struct
import time
import zlib
from collections import OrderedDict
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.report_periods import (
    completed_tehran_week_for_trigger,
    current_tehran_day,
    current_tehran_week,
    latest_completed_tehran_day,
    latest_weekly_trigger,
)
from schemas.media import DownloadError

try:
    TEHRAN = ZoneInfo("Asia/Tehran")
except ZoneInfoNotFoundError:
    # Windows Python may have no IANA data. Current Tehran scheduling uses
    # UTC+03:30; a host timezone database takes precedence whenever available.
    TEHRAN = timezone(timedelta(hours=3, minutes=30))


class ManagementService:
    def __init__(self, admin, groups, bot_id, *, weekly_weekday=5, weekly_hour=12):
        self.admin, self.groups, self.bot_id = admin, groups, bot_id
        self.weekly_weekday, self.weekly_hour = weekly_weekday, weekly_hour
        self.render_slots = asyncio.Semaphore(2)
        self.refused_routes = OrderedDict()
        self.route_generation = getattr(admin.channels, "generation", 0)

    def route_refused(self, source, target):
        generation = getattr(self.admin.channels, "generation", 0)
        if generation != self.route_generation:
            self.refused_routes.clear()
            self.route_generation = generation
        expiry = self.refused_routes.get((source, target))
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            self.refused_routes.pop((source, target), None)
            return False
        return True

    def refuse_route(self, source, target):
        self.route_refused(source, target)
        self.refused_routes[(source, target)] = time.monotonic() + 300
        self.refused_routes.move_to_end((source, target))
        while len(self.refused_routes) > 10000:
            self.refused_routes.popitem(last=False)

    async def developer_credit(self):
        identity = (await self.admin.channels.cached())["developer_id"]
        if not identity:
            return None
        return (
            f"👨‍💻 توسعه‌دهنده: tg://user?id={identity}"
            if type(identity) is int
            else f"👨‍💻 توسعه‌دهنده: {identity}"
        )

    async def enqueue_storage(
        self,
        source_peer,
        source_id,
        kind,
        sender_id,
        chat_title=None,
        username=None,
        is_private=False,
        *,
        sender_name=None,
    ):
        if is_private or (
            type(source_peer) is not int
            or source_peer == 0
            or type(source_id) is not int
            or source_id <= 0
            or (sender_id is not None and (type(sender_id) is not int or sender_id == 0))
        ):
            return 0
        config = await self.admin.channels.cached()
        destinations = {}
        if config["storage"] and kind in config["storage_kinds"]:
            destinations[config["storage"]["id"]] = "storage"
        for watch in config["watches"]:
            if watch["chat_id"] == source_peer and kind in watch["kinds"]:
                destinations[watch["channel"]["id"]] = "watch"
        if not destinations:
            return 0
        source_group = await self.groups.get(source_peer)
        if (
            not source_group
            or source_group.is_active is not True
            or source_group.migrated_to is not None
        ):
            return 0
        from core.jalali import to_persian_digits, to_shamsi

        divider = "➖➖➖➖➖➖➖➖➖➖"
        names = {
            "text": "پیام عادی",
            "photo": "عکس",
            "video": "فیلم",
            "voice": "وویس",
            "gif": "گیف",
            "sticker": "استیکر",
            "music": "موزیک",
        }
        footer = "\n".join(
            (
                "🗂 پیام ذخیره‌شده",
                divider,
                f"📨 نوع پیام: {names.get(kind, kind)}",
                f"🕐 زمان: {to_shamsi(datetime.now(UTC))}",
                divider,
                "",
                "🔹 فرستنده",
                f"👤 نام: {(sender_name or 'نامشخص')[:256]}",
                f"🔗 آیدی: {'@' + username[:64] if username else 'ندارد'}",
                f"🆔 آیدی عددی: {sender_id if sender_id is not None else 'نامشخص'}",
                "",
                "🔹 گپ",
                f"📛 اسم: {(source_group.title or chat_title or 'نامشخص')[:512]}",
                f"🆔 آیدی عددی: {source_peer}",
                f"👥 تعداد اعضا: {to_persian_digits(f'{source_group.member_count:,}').replace(',', '٬') if source_group.member_count is not None else 'نامشخص'}",
                f"🔗 لینک: {source_group.link or 'نامشخص'}",
            )
        )
        count = 0
        async with self.admin.uow_factory() as uow:
            for target, feature in destinations.items():
                if target == source_peer or self.route_refused(source_peer, target):
                    continue
                result = await uow.outbox.enqueue(
                    f"management:storage:{source_peer}:{source_id}:{target}",
                    "forward",
                    target,
                    {
                        "policy": "management",
                        "management": {
                            "feature": feature,
                            "source_peer": source_peer,
                            "kind": kind,
                        },
                        "source_peer": source_peer,
                        "source_id": source_id,
                        "footer": footer,
                    },
                )
                count += bool(result)
        return count

    async def archive_download(
        self,
        source_chat,
        sender_id,
        source_peer,
        source_id,
        site,
        content_id,
        quality,
        *,
        document=None,
        caption=None,
    ):
        config = await self.admin.channels.get()
        if (
            not config["archive"]
            or type(source_peer) is not int
            or type(source_id) is not int
            or source_id <= 0
        ):
            return False
        target = config["archive"]["id"]
        if target == source_peer:
            return False
        footer = f"📥 نتیجه دانلود · {site}\nشناسه محتوا: {content_id}\nکیفیت: {quality}\nدرخواست‌کننده: {sender_id}\nگفتگو: {source_chat}"
        payload = {
            "source_peer": source_peer,
            "source_id": source_id,
            "footer": footer[:3500],
            "policy": "management",
            "management": {"feature": "archive"},
        }
        kind = "forward"
        if document is not None:
            if (
                not isinstance(document, dict)
                or set(document) != {"document_id", "access_hash", "file_reference"}
                or any(type(document[key]) is not int for key in ("document_id", "access_hash"))
                or not isinstance(document["file_reference"], str)
                or len(document["file_reference"]) > 200000
            ):
                raise ValueError("Invalid archived Telegram document")
            base64.b64decode(document["file_reference"], validate=True)
            text = footer + ("\n\n" + caption if isinstance(caption, str) and caption else "")
            payload = {
                "document": document,
                "caption": text.encode("utf-16-le")[:2048].decode("utf-16-le", errors="ignore"),
                "policy": "management",
                "management": {"feature": "archive"},
            }
            kind = "archive_document"
        async with self.admin.uow_factory() as uow:
            return bool(
                await uow.outbox.enqueue(
                    f"management:archive:{source_peer}:{source_id}:{target}",
                    kind,
                    target,
                    payload,
                )
            )

    async def notice(self, key, target, text, reply_to=None):
        if (
            type(target) is not int
            or target == 0
            or not isinstance(text, str)
            or not 1 <= len(text) <= 3500
        ):
            raise ValueError("Invalid management notice")
        async with self.admin.uow_factory() as uow:
            return await uow.outbox.enqueue(
                "management:" + key,
                "message",
                target,
                {
                    "text": text,
                    "reply_to": reply_to,
                    "policy": "management",
                    "management": {"feature": "footer"},
                },
            )

    async def allowed(self, effect):
        config = await self.admin.channels.get()
        data = effect.payload.get("management", {})
        feature, target = data.get("feature"), effect.target_id
        if feature in {"group_lifecycle", "manual_report"}:
            group = await self.groups.get(target, fresh=True)
            if not group or group.is_active is not True or group.can_send is False:
                return False
            if feature == "group_lifecycle" and effect.payload.get("group_announcement") == "admin":
                return group.is_admin is True
            if (
                feature == "group_lifecycle"
                and effect.payload.get("group_announcement") == "demoted"
            ):
                return group.is_admin is False
            if feature == "group_lifecycle" and effect.payload.get("group_announcement") == "added":
                return group.is_admin is not True
            return True
        if feature in {"promo", "admin_nudge", "weekly_report"}:
            if not config[feature]["enabled"]:
                return False
            group = await self.groups.get(target, fresh=True)
            return bool(
                group
                and group.is_active is True
                and group.can_send is not False
                and (feature != "admin_nudge" or group.is_admin is not True)
            )
        if feature == "archive":
            return bool(config["archive"] and config["archive"]["id"] == target)
        if feature in {"storage", "watch"}:
            if self.route_refused(data.get("source_peer"), target):
                return False
            group = await self.groups.get(data.get("source_peer"), fresh=True)
            global_enabled = bool(
                config["storage"]
                and config["storage"]["id"] == target
                and data.get("kind") in config["storage_kinds"]
            )
            watch_enabled = any(
                row["chat_id"] == data.get("source_peer")
                and row["channel"]["id"] == target
                and data.get("kind") in row["kinds"]
                for row in config["watches"]
            )
            return bool(
                group
                and group.is_active is True
                and group.migrated_to is None
                and (global_enabled or watch_enabled)
            )
        if feature == "footer":
            return target in {
                channel["id"] for channel in (config["archive"], config["storage"]) if channel
            } | {watch["channel"]["id"] for watch in config["watches"]}
        if feature == "watch_removed":
            if target == self.admin.main_admin_id:
                return True
            source = data.get("source_peer")
            if (
                type(source) is not int
                or source >= 0
                or target != data.get("retired_target")
                or target >= 0
            ):
                return False
            group = await self.groups.get(source, fresh=True)
            return bool(group and group.is_active is False)
        return False

    async def tick(self, now=None):
        owner = self.admin.main_admin_id
        if not owner:
            return False
        await self.admin.authorize(owner, root=True)
        now = now or datetime.now(UTC)
        config = await self.admin.channels.get()
        candidates = []
        await self.rollover(now)
        for feature, kind in (("promo", "broadcast"), ("admin_nudge", "nudge_admin")):
            item = config[feature]
            if item["enabled"]:
                period = int(now.timestamp()) // (item["hours"] * 3600)
                payload = {
                    "audience": "groups",
                    "source_peer": owner,
                    "policy": "management",
                    "management": {"feature": feature},
                }
                if feature == "promo":
                    payload["text"] = item["text"]
                candidates.append((feature, kind, period, payload))
        if config["weekly_report"]["enabled"]:
            anchor, week_key = latest_weekly_trigger(
                now, weekday=self.weekly_weekday, hour=self.weekly_hour
            )
            week = completed_tehran_week_for_trigger(anchor)
            day = latest_completed_tehran_day(now)
            candidates.append(
                (
                    "weekly_report",
                    "report",
                    week_key,
                    {
                        "audience": "groups",
                        "source_peer": owner,
                        "report_start": week.start.isoformat(),
                        "report_end": week.end.isoformat(),
                        "policy": "management",
                        "management": {
                            "feature": "weekly_report",
                            "period": anchor.date().isoformat(),
                        },
                    },
                )
            )
            candidates.append(
                (
                    "daily_report",
                    "report",
                    day.key,
                    {
                        "audience": "groups",
                        "source_peer": owner,
                        "policy": "management",
                        "management": {"feature": "weekly_report"},
                        "report_period": "day",
                        "report_start": day.start.isoformat(),
                        "report_end": day.end.isoformat(),
                    },
                )
            )
        async with self.admin.uow_factory() as uow:
            for feature, kind, period, payload in candidates:
                self.admin.validate_job(kind, payload)
                await uow.admin.enqueue_job(owner, kind, payload, f"management:{feature}:{period}")
        return bool(candidates)

    async def rollover(self, now):
        local = now.astimezone(TEHRAN)
        day = local.date() - timedelta(days=1)
        if (local.hour == 0 and local.minute < 2) or getattr(self, "_sealed_day", None) == day:
            return
        async with self.admin.uow_factory() as uow:
            started = await uow.admin.setting("daily_statistics_started_at")
            if not started:
                await uow.admin.set_setting("daily_statistics_started_at", now.isoformat())
                return
            if day < datetime.fromisoformat(started).astimezone(TEHRAN).date():
                return
            existing = await uow.admin.connection.fetchval(
                "SELECT 1 FROM daily_stats WHERE telegram_account_id=$1 AND stat_date=$2",
                self.bot_id,
                day,
            )
            if not existing:
                await uow.daily_stats.save(day)
        self._sealed_day = day

    async def report(self, chat_id, *, period="week", start=None, end=None):
        if period not in {"day", "week"}:
            raise ValueError("Invalid report period")
        window = current_tehran_day() if period == "day" else current_tehran_week()
        since = datetime.fromisoformat(start) if start else window.start
        until = datetime.fromisoformat(end) if end else window.end
        if (
            since.tzinfo is None
            or until.tzinfo is None
            or not since < until
            or until - since > timedelta(days=8)
        ):
            raise ValueError("Invalid report window")
        group = await self.groups.get(chat_id, fresh=True)
        if not group or group.is_active is not True:
            raise DownloadError("گروه برای گزارش در دسترس نیست.")
        row = await self.admin.repository.connection.fetchrow(
            """SELECT count(*) FILTER(WHERE is_active) members,
            COALESCE(sum(message_count),0)::bigint messages,
            (SELECT count(DISTINCT e.user_id) FROM membership_events e WHERE e.telegram_account_id=$1 AND e.chat_id=$2 AND e.kind='message' AND e.occurred_at>=$3 AND e.occurred_at<$4) weekly_active,
            (SELECT count(*) FROM membership_events e WHERE e.telegram_account_id=$1 AND e.chat_id=$2 AND e.kind='message' AND e.occurred_at>=$3 AND e.occurred_at<$4) weekly_messages
            FROM group_memberships WHERE telegram_account_id=$1 AND chat_id=$2""",
            self.bot_id,
            chat_id,
            since,
            until,
        )
        values = dict(row)
        values["period"] = period
        async with self.render_slots:
            data = await asyncio.to_thread(render_report, values)
        label = "امروز" if period == "day" else "هفته"
        caption = f"📊 گزارش {label} · {(group.title or str(chat_id))[:160]}\nاعضای فعال ثبت‌شده: {values['members']}\nکاربران پیام‌دهنده: {values['weekly_active']}\nپیام‌های {label}: {values['weekly_messages']}\nکل پیام‌های ثبت‌شده: {values['messages']}\nآمار بر اساس داده‌های قابل مشاهدهٔ ربات است."
        from core.jalali import to_shamsi

        caption += "\nبازه: " + to_shamsi(since) + " تا " + to_shamsi(until)
        return data, caption

    async def request_report(self, user_id, chat_id, period, reply_to=None):
        import uuid

        if period not in {"day", "week"} or type(chat_id) is not int or chat_id >= 0:
            raise ValueError("Invalid group report")
        async with self.admin.uow_factory() as uow:
            root = user_id == self.admin.main_admin_id
            if not root:
                granted = await uow.admin.connection.fetchval(
                    """INSERT INTO request_limits(telegram_account_id,user_id,scope,requested_at)
                VALUES($1,$2,$3,clock_timestamp()) ON CONFLICT(telegram_account_id,user_id,scope) DO UPDATE SET requested_at=EXCLUDED.requested_at
                WHERE request_limits.requested_at<clock_timestamp()-interval '24 hours' RETURNING 1""",
                    self.bot_id,
                    chat_id,
                    "manual-report:" + period,
                )
                if not granted:
                    return False
            await uow.outbox.enqueue(
                "manual-report:" + uuid.uuid4().hex,
                "report",
                chat_id,
                {
                    "report_period": period,
                    "report_start": (
                        current_tehran_day() if period == "day" else current_tehran_week()
                    ).start.isoformat(),
                    "report_end": datetime.now(UTC).isoformat(),
                    "requested_by": user_id,
                    "reply_to": reply_to,
                    "policy": "management",
                    "management": {"feature": "manual_report"},
                },
            )
        return True


FONT = {
    "0": (14, 17, 19, 21, 25, 17, 14),
    "1": (4, 12, 4, 4, 4, 4, 14),
    "2": (14, 17, 1, 2, 4, 8, 31),
    "3": (30, 1, 1, 14, 1, 1, 30),
    "4": (2, 6, 10, 18, 31, 2, 2),
    "5": (31, 16, 16, 30, 1, 1, 30),
    "6": (14, 16, 16, 30, 17, 17, 14),
    "7": (31, 1, 2, 4, 8, 8, 8),
    "8": (14, 17, 17, 14, 17, 17, 14),
    "9": (14, 17, 17, 15, 1, 1, 14),
    "A": (14, 17, 17, 31, 17, 17, 17),
    "B": (30, 17, 17, 30, 17, 17, 30),
    "C": (14, 17, 16, 16, 16, 17, 14),
    "E": (31, 16, 16, 30, 16, 16, 31),
    "G": (14, 17, 16, 23, 17, 17, 15),
    "I": (14, 4, 4, 4, 4, 4, 14),
    "K": (17, 18, 20, 24, 20, 18, 17),
    "L": (16, 16, 16, 16, 16, 16, 31),
    "M": (17, 27, 21, 21, 17, 17, 17),
    "N": (17, 25, 21, 19, 17, 17, 17),
    "O": (14, 17, 17, 17, 17, 17, 14),
    "P": (30, 17, 17, 30, 16, 16, 16),
    "R": (30, 17, 17, 30, 20, 18, 17),
    "S": (15, 16, 16, 14, 1, 1, 30),
    "T": (31, 4, 4, 4, 4, 4, 4),
    "U": (17, 17, 17, 17, 17, 17, 14),
    "V": (17, 17, 17, 17, 17, 10, 4),
    "W": (17, 17, 17, 21, 21, 21, 10),
    "Y": (17, 17, 10, 4, 4, 4, 4),
    " ": (0, 0, 0, 0, 0, 0, 0),
}


def render_report(values):
    """Render an original bitmap card with no optional font or plotting runtime."""
    width, height = 900, 480
    pixels = bytearray(bytes((12, 22, 39)) * width * height)

    def rectangle(x, y, w, h, color):
        for row in range(max(0, y), min(height, y + h)):
            left, right = max(0, x), min(width, x + w)
            if left < right:
                offset = (row * width + left) * 3
                pixels[offset : offset + (right - left) * 3] = bytes(color) * (right - left)

    def text(label, x, y, scale=3, color=(230, 240, 255)):
        for character in str(label):
            for row, bits in enumerate(FONT.get(character, FONT[" "])):
                for column in range(5):
                    if bits & (1 << (4 - column)):
                        rectangle(x + column * scale, y + row * scale, scale, scale, color)
            x += 6 * scale

    daily = values.get("period") == "day"
    text("DAILY GROUP REPORT" if daily else "WEEKLY GROUP REPORT", 36, 28, 4)
    entries = (
        ("MEMBERS", values["members"]),
        ("ACTIVE", values["weekly_active"]),
        ("DAY MESSAGES" if daily else "WEEK MESSAGES", values["weekly_messages"]),
        ("ALL MESSAGES", values["messages"]),
    )
    ceiling = max(1, *(int(value) for _, value in entries))
    for index, (label, value) in enumerate(entries):
        y = 110 + index * 87
        text(label, 36, y)
        text(str(value)[:18], 570, y)
        rectangle(36, y + 34, 820, 18, (35, 48, 67))
        rectangle(36, y + 34, max(3, int(820 * int(value) / ceiling)), 18, (55, 189, 160))
    scanlines = b"".join(
        b"\x00" + pixels[row * width * 3 : (row + 1) * width * 3] for row in range(height)
    )

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines, 6))
        + chunk(b"IEND", b"")
    )
