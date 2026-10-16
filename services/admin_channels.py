"""Typed, audited channel and scheduling configuration owned by the bot account."""

import asyncio
import time
from copy import deepcopy

from schemas.media import DownloadError

DEFAULTS = {
    "forced_channels": [],
    "join_points": ["start", "usage", "recheck"],
    "archive": None,
    "storage": None,
    "storage_kinds": ["text", "photo", "video", "voice", "gif", "sticker", "music"],
    "watches": [],
    "promo": {
        "enabled": False,
        "hours": 6,
        "text": "لینک رسانه را در همین گروه بفرست تا فایل را دریافت کنی. 📥",
    },
    "admin_nudge": {"enabled": False, "hours": 24},
    "weekly_report": {"enabled": False},
    "developer_id": None,
}


class ChannelSettings:
    KEY = "channel_management"

    def __init__(self, admin):
        self.admin = admin
        self._cached = None
        self._expires = 0
        self._lock = asyncio.Lock()

    async def cached(self):
        """One bounded snapshot for routing; delivery always reads SQL fresh."""
        if self._cached is not None and self._expires > time.monotonic():
            return self._cached

        async with self._lock:
            if self._cached is None or self._expires <= time.monotonic():
                self._cached = await self.get()
                self._expires = time.monotonic() + 5
            return self._cached

    def invalidate(self):
        self._cached = None
        self._expires = 0

    async def get(self, owner=None):
        if owner is not None:
            await self.admin.authorize(owner)
        value = await self.admin.repository.setting(self.KEY, {})
        return {**deepcopy(DEFAULTS), **(value if isinstance(value, dict) else {})}

    async def update(self, owner, key, value):
        return await self.modify(owner, key, lambda previous: value)

    async def modify(self, owner, key, transform):
        await self.admin.authorize(owner, root=True)
        if key not in DEFAULTS:
            raise DownloadError("تنظیم کانال معتبر نیست.")
        async with self.admin.uow_factory() as uow:
            # Serialize independent settings edits, including the first insert.
            await uow.admin.connection.execute(
                "SELECT pg_advisory_xact_lock($1,$2)",
                self.admin.repository.bot_id % 2147483647,
                204217,
            )
            current = await uow.admin.setting(self.KEY, {})
            current = current if isinstance(current, dict) else {}
            value = transform(deepcopy(current.get(key, DEFAULTS[key])))
            self.validate(key, value)
            await uow.admin.set_setting(self.KEY, {**current, key: value})
            await uow.admin.audit(owner, "channel_setting", details={"key": key})
        self.invalidate()
        return value

    async def upsert_channel(self, owner, key, channel):
        if key != "forced_channels":
            raise DownloadError("فهرست کانال معتبر نیست.")
        return await self.modify(
            owner, key, lambda rows: [row for row in rows if row["id"] != channel["id"]] + [channel]
        )

    async def remove_channel(self, owner, identity):
        return await self.modify(
            owner, "forced_channels", lambda rows: [row for row in rows if row["id"] != identity]
        )

    async def upsert_watch(self, owner, watch):
        def replace(rows):
            previous = next((row for row in rows if row["chat_id"] == watch["chat_id"]), None)
            replacement = {**watch, "kinds": previous["kinds"] if previous else watch["kinds"]}
            return [row for row in rows if row["chat_id"] != watch["chat_id"]] + [replacement]

        return await self.modify(
            owner,
            "watches",
            replace,
        )

    async def remove_watch(self, owner, identity):
        return await self.modify(
            owner, "watches", lambda rows: [row for row in rows if row["chat_id"] != identity]
        )

    @staticmethod
    def validate(key, value):
        invalid = DownloadError("تنظیم کانال معتبر نیست.")
        if key not in DEFAULTS:
            raise invalid
        if key in {"archive", "storage"}:
            if value is not None and not valid_channel(value):
                raise invalid
        elif key == "forced_channels":
            if (
                not isinstance(value, list)
                or len(value) > 50
                or any(not valid_channel(item) for item in value)
                or len({item["id"] for item in value}) != len(value)
            ):
                raise invalid
        elif key in {"join_points", "storage_kinds"}:
            choices = DEFAULTS[key]
            if (
                not isinstance(value, list)
                or any(item not in choices for item in value)
                or len(set(value)) != len(value)
            ):
                raise invalid
        elif key == "watches":
            if not isinstance(value, list) or len(value) > 1000:
                raise invalid
            for item in value:
                if (
                    not isinstance(item, dict)
                    or type(item.get("chat_id")) is not int
                    or item["chat_id"] >= 0
                    or not valid_channel(item.get("channel"))
                ):
                    raise invalid
                ChannelSettings.validate("storage_kinds", item.get("kinds"))
            if len({item["chat_id"] for item in value}) != len(value):
                raise invalid
        elif key in {"promo", "admin_nudge", "weekly_report"}:
            if (
                not isinstance(value, dict)
                or type(value.get("enabled")) is not bool
                or set(value) - set(DEFAULTS[key])
            ):
                raise invalid
            if key != "weekly_report" and (
                type(value.get("hours")) is not int or not 1 <= value["hours"] <= 24
            ):
                raise invalid
            if key == "promo" and (
                not isinstance(value.get("text"), str) or not 1 <= len(value["text"]) <= 3500
            ):
                raise invalid
        elif (
            key == "developer_id"
            and value is not None
            and (type(value) is not int or not 0 < value < 2**63)
        ):
            raise invalid


def valid_channel(value):
    return (
        isinstance(value, dict)
        and set(value) == {"id", "link", "title"}
        and type(value["id"]) is int
        and -(2**63) < value["id"] < 0
        and isinstance(value["link"], str)
        and value["link"].startswith("https://t.me/")
        and len(value["link"]) <= 512
        and isinstance(value["title"], str)
        and len(value["title"]) <= 256
    )
