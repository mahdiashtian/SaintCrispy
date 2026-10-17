"""Fresh authorization, audited operations and durable owner-scoped confirmations."""

import csv
import io
import logging

from repositories.interfaces.control import ControlRepository, ObjectCache
from schemas.admin import AdminDataFile, SettingSnapshot
from schemas.media import DownloadError

log = logging.getLogger("activity")
ROOT_ACTIONS = {
    "grant",
    "revoke",
    "ban",
    "unban",
    "limit",
    "user_quota",
    "group_quota",
    "pause",
    "notifications",
    "broadcast",
    "forward",
    "leave",
    "import_users",
}


class AdminService:
    def __init__(
        self,
        repository: ControlRepository,
        uow_factory,
        groups,
        main_admin_id=None,
        cache: ObjectCache | None = None,
    ):
        self.repository, self.uow_factory, self.groups = repository, uow_factory, groups
        self.main_admin_id, self.cache = main_admin_id, cache
        from services.admin_channels import ChannelSettings

        self.channels = ChannelSettings(self)
        from services.admin_maintenance import AdminMaintenance

        self.maintenance = AdminMaintenance(self)
        self.roster = None

    async def crawl_progress(self, owner, chat_id):
        from dataclasses import asdict

        await self.authorize(owner)
        if self.roster is None:
            raise DownloadError("خزش اعضا در تنظیمات غیرفعال است.")
        value = await self.roster.progress(chat_id)
        if value is None:
            return None
        return {
            **asdict(value),
            "coverage_ratio": value.coverage_ratio,
            "is_complete": value.is_complete,
            "accounted_for": value.accounted_for,
        }

    async def administrators(self, owner):
        await self.authorize(owner)
        return await self.repository.administrators()

    async def group_directory(self, owner, page=0, active=None, order="date"):
        await self.authorize(owner)
        return await self.repository.group_directory(page, active, order)

    async def member_directory(self, owner, chat_id, page=0, active="all", started="all"):
        await self.authorize(owner)
        if type(chat_id) is not int or chat_id >= 0:
            raise DownloadError("شناسه گروه معتبر نیست.")
        return await self.repository.member_directory(chat_id, page, active, started)

    async def clear_prompts(self, owner):
        await self.repository.clear_prompts(owner)

    async def authorize(self, user_id, *, root=False, repository=None):
        if type(user_id) is not int or user_id <= 0:
            raise DownloadError("دسترسی مدیریت مجاز نیست.", code="admin_denied")
        if user_id == self.main_admin_id:
            return True
        if not root and await (repository or self.repository).authorized(user_id):
            return False
        raise DownloadError("این بخش فقط برای مدیر مجاز ربات است.", code="admin_denied")

    async def check_download(self, user_id):
        if user_id == self.main_admin_id:
            return
        if await self.repository.banned(user_id):
            raise DownloadError("دسترسی دریافت فایل برای این حساب غیرفعال شده.", code="user_banned")
        if await self.repository.setting("downloads_enabled", True) is not True:
            raise DownloadError(
                "دریافت فایل موقتاً برای نگهداری غیرفعال است.", code="downloads_paused"
            )

    async def can_contact(self, user_id):
        user = await self.repository.user(user_id)
        return bool(user and user.is_started and not user.is_blocked and not user.is_banned)

    async def stats(self, owner):
        await self.authorize(owner)
        return await self.repository.stats()

    async def daily(self, owner):
        await self.authorize(owner)
        return await self.repository.daily()

    async def daily_summary(self, owner):
        from datetime import datetime, timedelta, timezone

        await self.authorize(owner)
        return await self.repository.daily_summary(
            datetime.now(timezone(timedelta(hours=3, minutes=30))).date()
        )

    async def history(self, owner, limit=14, offset=0):
        await self.authorize(owner)
        return await self.repository.stats_history(limit, offset)

    async def history_count(self, owner):
        await self.authorize(owner)
        return await self.repository.history_count()

    async def users(self, owner, cursor=0):
        await self.authorize(owner)
        return await self.repository.users(cursor)

    async def groups_page(self, owner, cursor=None):
        await self.authorize(owner)
        return await self.groups.page(cursor)

    async def user(self, owner, query):
        await self.authorize(owner)
        if self.cache and type(query) is int:
            return await self.cache.read("user", query, lambda: self.repository.user(query))
        return await self.repository.user(query)

    async def trace(self, owner, identity):
        await self.authorize(owner)
        return await self.repository.user_trace(identity)

    async def public_trace(self, owner, identity):
        await self.authorize(owner)
        return await self.repository.public_trace(identity)

    async def download_page(self, owner, identity, page=0):
        await self.authorize(owner)
        if (
            type(identity) is not int
            or identity <= 0
            or type(page) is not int
            or not 0 <= page <= 1000000
        ):
            raise DownloadError("صفحهٔ دریافت‌ها معتبر نیست.")
        return await self.repository.download_page(identity, page)

    async def group(self, owner, identity):
        await self.authorize(owner)
        return await self.groups.get(identity, fresh=True)

    async def members(self, owner, identity, cursor=0):
        await self.authorize(owner)
        return await self.repository.members(identity, cursor)

    async def jobs(self, owner):
        await self.authorize(owner)
        return await self.repository.jobs()

    async def mutate(self, owner, action, target=None, value=None):
        await self.authorize(owner, root=True)
        async with self.uow_factory() as uow:
            if action in {"grant", "revoke", "ban", "unban"}:
                if type(target) is not int or target <= 0 or target == self.main_admin_id:
                    raise DownloadError("شناسه معتبرِ غیر از مدیر اصلی را بفرست.")
                field = "is_admin" if action in {"grant", "revoke"} else "is_banned"
                try:
                    await uow.admin.change_user(target, field, action in {"grant", "ban"})
                except ValueError as error:
                    raise DownloadError(
                        "این حساب قبلاً ربات یا حذف‌شده تشخیص داده شده است."
                    ) from error
            elif action == "limit":
                if (
                    type(target) is not int
                    or target >= 0
                    or type(value) is not int
                    or not 1 <= value <= 1000
                ):
                    raise DownloadError("شناسه گروه و سقف ۱ تا ۱۰۰۰ را وارد کن.")
                if not await uow.admin.group_limit(target, value):
                    raise DownloadError("این گروه هنوز ثبت نشده است.")
            elif action in {"user_quota", "group_quota"}:
                if type(value) is not int or not 1 <= value <= 1000:
                    raise DownloadError("سقف باید عددی از ۱ تا ۱۰۰۰ باشد.")
                await uow.admin.set_setting(
                    "user_links_per_minute" if action == "user_quota" else "group_links_per_minute",
                    value,
                )
            elif action in {"pause", "notifications"}:
                if type(value) is not bool:
                    raise DownloadError("مقدار تنظیم معتبر نیست.")
                await uow.admin.set_setting(
                    "downloads_enabled" if action == "pause" else "lifecycle_notifications", value
                )
            else:
                raise DownloadError("عملیات مدیریت معتبر نیست.")
            await uow.admin.audit(
                owner, action, target, {"value": value} if value is not None else {}
            )
        if action == "limit" and self.cache:
            await self.cache.invalidate("group", target)
        if action in {"grant", "revoke", "ban", "unban"} and self.cache:
            await self.cache.invalidate("user", target)
        if action in {"pause", "notifications"} and self.cache:
            await self.cache.invalidate(
                "setting", "downloads_enabled" if action == "pause" else "lifecycle_notifications"
            )
        log.info(
            {"event": "admin_action", "actor_id": owner, "action": action, "target_id": target}
        )

    async def prompt(self, owner, action, payload=None):
        await self.authorize(owner, root=action in ROOT_ACTIONS)
        if action not in ROOT_ACTIONS | {"lookup", "group_lookup"}:
            raise DownloadError("فرم معتبر نیست.")
        return await self.repository.request(owner, "prompt", action, payload or {})

    async def attach_prompt(self, owner, token, message_id):
        await self.authorize(owner)
        await self.repository.attach_prompt(owner, token, message_id)

    async def find_prompt(self, owner, message_id):
        await self.authorize(owner)
        return await self.repository.prompt(owner, message_id)

    async def consume_prompt(self, owner, token):
        async with self.uow_factory() as uow:
            await self.authorize(owner, repository=uow.admin)
            row = await uow.admin.consume(owner, token)
            if row is None or row["kind"] != "prompt":
                raise DownloadError("این فرم منقضی یا استفاده شده است.")
            await self.authorize(owner, root=row["action"] in ROOT_ACTIONS, repository=uow.admin)
        return row["action"], row["payload"]

    async def options(self, owner):
        root = await self.authorize(owner)

        async def option(key):
            async def load():
                return SettingSnapshot(key, await self.repository.setting(key, True))

            return (
                await self.cache.read("setting", key, load) if self.cache else await load()
            ).value

        return {
            "root": root,
            "downloads_enabled": await option("downloads_enabled"),
            "lifecycle_notifications": await option("lifecycle_notifications"),
            "user_links_per_minute": await self.repository.setting("user_links_per_minute", 1),
            "group_links_per_minute": await self.repository.setting(
                "group_links_per_minute", self.groups.links_per_minute
            ),
        }

    def validate_job(self, kind, payload):
        if kind not in {
            "broadcast",
            "forward",
            "health",
            "leave",
            "crawl",
            "nudge_admin",
            "nudge_owners",
            "refresh_users",
            "report",
            "purge_users",
        } or not isinstance(payload, dict):
            raise DownloadError("درخواست مدیریت معتبر نیست.")
        if kind == "nudge_owners" and (
            payload.get("target", "owner") not in {"owner", "admins", "both"}
            or type(payload.get("with_name", True)) is not bool
        ):
            raise DownloadError("گزینه یادآوری معتبر نیست.")
        if kind in {"broadcast", "forward"}:
            if payload.get("group_audience", "both") not in {"both", "admin", "non_admin"}:
                raise DownloadError("مخاطبان معتبر نیستند.")
            excluded_members = payload.get("exclude_members", [])
            if (
                not isinstance(excluded_members, list)
                or len(excluded_members) > 1000
                or any(type(i) is not int or not 0 < i < 2**63 for i in excluded_members)
            ):
                raise DownloadError("فهرست کاربران مستثنا معتبر نیست.")
            target = payload.get("target_id")
            if target is not None and (
                type(target) is not int or target == 0 or not -(2**63) < target < 2**63
            ):
                raise DownloadError("مقصد معتبر نیست.")
            if payload.get("audience") not in {"users", "groups"}:
                raise DownloadError("مخاطبان معتبر نیستند.")
            exclusions = payload.get("exclude", [])
            adders = payload.get("exclude_adders", [])
            if (
                not isinstance(adders, list)
                or len(adders) > 1000
                or any(type(i) is not int or not 0 < i < 2**63 for i in adders)
            ):
                raise DownloadError("آورنده‌های مستثنا معتبر نیستند.")
            if (
                not isinstance(exclusions, list)
                or len(exclusions) > 1000
                or any(type(i) is not int or i == 0 for i in exclusions)
            ):
                raise DownloadError("فهرست استثنا معتبر نیست.")
        if (
            kind == "broadcast"
            and not payload.get("copy")
            and (not isinstance(payload.get("text"), str) or not 1 <= len(payload["text"]) <= 3500)
        ):
            raise DownloadError("متن ارسال باید بین ۱ تا ۳۵۰۰ نویسه باشد.")
        if (kind == "forward" or payload.get("copy")) and (
            type(payload.get("source_id")) is not int or payload["source_id"] <= 0
        ):
            raise DownloadError("روی پیام قابل فوروارد ریپلای کن.")
        if kind == "leave" and (type(payload.get("chat_id")) is not int or payload["chat_id"] >= 0):
            raise DownloadError("شناسه گروه معتبر نیست.")
        if (
            kind in {"health", "crawl"}
            and payload.get("chat_id") is not None
            and (type(payload["chat_id"]) is not int or payload["chat_id"] >= 0)
        ):
            raise DownloadError("شناسه گروه معتبر نیست.")

    async def prepare(self, owner, kind, payload):
        await self.authorize(owner, root=kind != "health")
        self.validate_job(kind, payload)
        payload = {**payload, "source_peer": owner}
        return await self.repository.request(owner, "confirm", kind, payload)

    async def confirm(self, owner, token):
        async with self.uow_factory() as uow:
            await self.authorize(owner, repository=uow.admin)
            row = await uow.admin.consume(owner, token)
            if row is None or row["kind"] != "confirm":
                raise DownloadError("این تأیید منقضی یا قبلاً استفاده شده است.")
            await self.authorize(owner, root=row["action"] != "health", repository=uow.admin)
            self.validate_job(row["action"], row["payload"])
            job_id = await uow.admin.enqueue_job(
                owner, row["action"], row["payload"], "confirmation:" + token
            )
            await uow.admin.audit(owner, row["action"], details={"job_id": job_id})
        return job_id

    async def cancel(self, owner, job_id):
        await self.authorize(owner, root=True)
        async with self.uow_factory() as uow:
            result = await uow.admin.cancel_job(job_id)
            await uow.admin.audit(owner, "cancel_job", details={"job_id": job_id})
        return bool(result)

    async def recipient_unavailable(self, user_id, reason):
        async with self.uow_factory() as uow:
            await uow.admin.mark_blocked(user_id, reason == "InputUserDeactivatedError")
        if self.cache:
            await self.cache.invalidate("user", user_id)

    async def cache_status(self, owner):
        await self.authorize(owner)
        result = {
            "enabled": bool(self.cache and self.cache.redis is not None),
            "reachable": False,
            "dirty": len(self.cache.dirty) if self.cache else 0,
        }
        if result["enabled"]:
            try:
                result["reachable"] = bool(await self.cache.redis.ping())
            except Exception:
                pass
        return result

    async def export(self, owner, kind, cursor=None):
        await self.authorize(owner, root=True)
        if kind == "users":
            rows = await self.repository.users(cursor or 0, 500)
            fields = (
                "user_id",
                "username",
                "first_name",
                "is_started",
                "is_admin",
                "is_banned",
                "is_blocked",
            )
            next_cursor = rows[-1].user_id if len(rows) == 500 else None
        elif kind == "groups":
            rows = await self.groups.page(cursor, 500)
            fields = (
                "chat_id",
                "title",
                "is_active",
                "is_admin",
                "can_send",
                "added_by_user_id",
                "owner_id",
                "member_count",
            )
            next_cursor = rows[-1].chat_id if len(rows) == 500 else None
        else:
            raise DownloadError("نوع خروجی معتبر نیست.")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(fields)
        for row in rows:
            values = []
            for key in fields:
                value = getattr(row, key)
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    value = "'" + value
                values.append(value)
            writer.writerow(values)
        async with self.uow_factory() as uow:
            await uow.admin.audit(owner, "export_" + kind, details={"rows": len(rows)})
        return AdminDataFile(f"{kind}-page.csv", output.getvalue().encode("utf-8-sig"), next_cursor)

    async def import_users(self, owner, content):
        await self.authorize(owner, root=True)
        if not isinstance(content, bytes) or len(content) > 1024 * 1024:
            raise DownloadError("فایل CSV باید حداکثر یک مگابایت باشد.")
        try:
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
            if (
                not reader.fieldnames
                or len(reader.fieldnames) != len(set(reader.fieldnames))
                or set(reader.fieldnames) - {"user_id", "username", "first_name", "last_name"}
                or "user_id" not in reader.fieldnames
            ):
                raise ValueError("Invalid CSV fields")
            rows = []
            seen = set()
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Invalid CSV column count")
                identity = int(row["user_id"])
                names = [row.get(key) or None for key in ("username", "first_name", "last_name")]
                if (
                    not 0 < identity < 2**63
                    or identity in seen
                    or any(value is not None and len(value) > 256 for value in names)
                    or any(value is not None and "\x00" in value for value in names)
                ):
                    raise ValueError("Invalid CSV row")
                if len(rows) >= 500:
                    raise ValueError("CSV row cap")
                seen.add(identity)
                rows.append((identity, *names))
        except (ValueError, UnicodeError, TypeError, csv.Error) as error:
            raise DownloadError(
                "CSV معتبر با حداکثر ۵۰۰ ردیف و ستون user_id و نام‌ها بفرست؛ نقش و استارت قابل واردکردن نیستند."
            ) from error
        async with self.uow_factory() as uow:
            await uow.admin.import_users(rows)
            await uow.admin.audit(owner, "import_users", details={"rows": len(rows)})
        if self.cache:
            for row in rows:
                await self.cache.invalidate("user", row[0])
        return len(rows)
