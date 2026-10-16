"""Durable channel forwarding, opt-in schedules and account-scoped reports."""

import asyncio
import base64
import struct
import zlib
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

    async def developer_credit(self):
        identity = (await self.admin.channels.cached())["developer_id"]
        return f"👨‍💻 توسعه‌دهنده: tg://user?id={identity}" if identity else None

    async def enqueue_storage(
        self,
        source_peer,
        source_id,
        kind,
        sender_id,
        chat_title=None,
        username=None,
        is_private=False,
    ):
        if is_private or (
            type(source_peer) is not int
            or source_peer == 0
            or type(source_id) is not int
            or source_id <= 0
            or type(sender_id) is not int
            or sender_id <= 0
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
        footer = (
            f"📬 {'گفت‌وگوی خصوصی' if is_private else (chat_title or 'گروه')[:120]}\nشناسه گفتگو: {source_peer}\nکاربر: {sender_id}"
            + (f" · @{username[:64]}" if username else "")
        )
        count = 0
        async with self.admin.uow_factory() as uow:
            for target, feature in destinations.items():
                if target == source_peer:
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
            local = now.astimezone(TEHRAN)
            anchor = (local - timedelta(days=(local.weekday() - self.weekly_weekday) % 7)).replace(
                hour=self.weekly_hour, minute=0, second=0, microsecond=0
            )
            if anchor > local:
                anchor -= timedelta(days=7)
            candidates.append(
                (
                    "weekly_report",
                    "report",
                    anchor.date().isoformat(),
                    {
                        "audience": "groups",
                        "source_peer": owner,
                        "policy": "management",
                        "management": {
                            "feature": "weekly_report",
                            "period": anchor.date().isoformat(),
                        },
                    },
                )
            )
        async with self.admin.uow_factory() as uow:
            for feature, kind, period, payload in candidates:
                self.admin.validate_job(kind, payload)
                await uow.admin.enqueue_job(owner, kind, payload, f"management:{feature}:{period}")
        return bool(candidates)

    async def report(self, chat_id):
        group = await self.groups.get(chat_id, fresh=True)
        if not group or group.is_active is not True:
            raise DownloadError("گروه برای گزارش در دسترس نیست.")
        row = await self.admin.repository.connection.fetchrow(
            """SELECT count(*) FILTER(WHERE is_active) members,
            COALESCE(sum(message_count),0)::bigint messages,
            count(DISTINCT user_id) FILTER(WHERE is_active AND last_seen_at>=now()-interval '7 days') weekly_active,
            (SELECT count(*) FROM membership_events e WHERE e.telegram_account_id=$1 AND e.chat_id=$2 AND e.kind='message' AND e.occurred_at>=now()-interval '7 days') weekly_messages
            FROM group_memberships WHERE telegram_account_id=$1 AND chat_id=$2""",
            self.bot_id,
            chat_id,
        )
        values = dict(row)
        async with self.render_slots:
            data = await asyncio.to_thread(render_report, values)
        caption = f"📊 گزارش هفت روز اخیر · {(group.title or str(chat_id))[:160]}\nاعضای فعال ثبت‌شده: {values['members']}\nکاربران پیام‌دهنده: {values['weekly_active']}\nپیام‌های هفته: {values['weekly_messages']}\nکل پیام‌های ثبت‌شده: {values['messages']}\nآمار بر اساس داده‌های قابل مشاهدهٔ ربات است."
        return data, caption


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

    text("WEEKLY GROUP REPORT", 36, 28, 4)
    entries = (
        ("MEMBERS", values["members"]),
        ("ACTIVE", values["weekly_active"]),
        ("WEEK MESSAGES", values["weekly_messages"]),
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
