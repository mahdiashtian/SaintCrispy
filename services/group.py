"""Transactional lifecycle and admission policy for Telegram groups."""

import json
import logging
import uuid
from datetime import UTC, datetime

from repositories.interfaces.control import ObjectCache
from repositories.interfaces.group import GroupRepository
from schemas.group import GroupChange, GroupSnapshot
from schemas.media import DownloadError

from .base import TransactionalService

log = logging.getLogger("activity")


class GroupService(TransactionalService):
    def __init__(
        self,
        repository: GroupRepository,
        uow_factory,
        links_per_minute=5,
        *,
        cache: ObjectCache | None = None,
        main_admin_id=None,
        retry_queue=None,
    ):
        super().__init__(uow_factory)
        if not 1 <= links_per_minute <= 1000:
            raise ValueError("Group links per minute must be between 1 and 1000")
        self.repository = repository
        self.links_per_minute = links_per_minute
        self.cache, self.main_admin_id = cache, main_admin_id
        self.retry_queue, self.consumer = retry_queue, uuid.uuid4().hex

    async def get(self, chat_id: int, *, fresh=False) -> GroupSnapshot | None:
        if type(chat_id) is not int or chat_id >= 0:
            raise ValueError("A group must have a negative Telegram chat ID")
        if self.cache is not None and not fresh:
            return await self.cache.read("group", chat_id, lambda: self.repository.get(chat_id))
        return await self.repository.get(chat_id)

    async def page(self, cursor=None, limit=20):
        return await self.repository.page(cursor, limit)

    async def peer(self, chat_id):
        return await self.repository.peer(chat_id)

    async def remember_peer(self, chat_id, raw_id, chat_kind, access_hash):
        if (
            type(raw_id) is not int
            or raw_id <= 0
            or chat_kind not in {"group", "supergroup"}
            or chat_id != (-raw_id if chat_kind == "group" else -(10**12 + raw_id))
        ):
            raise ValueError("Unverified peer identity")
        if chat_kind == "supergroup" and (type(access_hash) is not int or not access_hash):
            return
        async with self._uow_factory() as uow:
            await uow.groups.remember_peer(chat_id, raw_id, chat_kind, access_hash)

    async def metadata(self, chat_id, owner_id, admin_ids, member_count, link):
        if any(type(value) is not int or value <= 0 for value in admin_ids) or len(admin_ids) > 200:
            raise ValueError("Invalid group administrator snapshot")
        async with self._uow_factory() as uow:
            await uow.groups.metadata(chat_id, owner_id, admin_ids, member_count, link)
        if self.cache:
            await self.cache.invalidate("group", chat_id)

    async def record(self, change: GroupChange, *, replaying=False):
        try:
            return await self._record(change)
        except Exception as error:
            if (
                replaying
                or self.retry_queue is None
                or not (
                    isinstance(error, (OSError, TimeoutError))
                    or type(error).__module__.startswith("asyncpg")
                )
            ):
                raise
            if not await self.retry_queue.append(change):
                raise
            log.error(
                {
                    "event": "group_lifecycle_deferred",
                    "chat_id": change.chat_id,
                    "kind": change.kind,
                    "error_type": type(error).__name__,
                }
            )
            return None

    async def flush_deferred(self):
        if self.retry_queue is None:
            return
        for shard in range(self.retry_queue.shards):
            for identity, fields in await self.retry_queue.batch(shard, self.consumer):
                try:
                    raw = fields.get(b"event", fields.get("event"))
                    if raw is None or len(raw) > 16384:
                        raise ValueError("Invalid lifecycle record")
                    change = GroupChange.load(json.loads(raw))
                    if self.retry_queue.shard(change.chat_id) != shard:
                        raise ValueError("Wrong lifecycle shard")
                except (ValueError, TypeError, KeyError, UnicodeError):
                    await self.retry_queue.quarantine(shard, identity)
                    await self.retry_queue.acknowledge(shard, [identity])
                    continue
                if change.kind == "migrated" and change.migrated_to is not None:
                    await self.migrate(
                        change.chat_id, change.migrated_to, change.occurred_at, replaying=True
                    )
                else:
                    await self.record(change, replaying=True)
                await self.retry_queue.acknowledge(shard, [identity])

    async def _record(self, change: GroupChange):
        async with self._uow_factory() as uow:
            result = await uow.groups.record(change)
            cached = await uow.groups.get(change.chat_id) if result and self.cache else None
            if (
                result
                and result["applied"]
                and result.get("transition")
                and self.main_admin_id
                and abs((datetime.now(UTC) - change.occurred_at).total_seconds()) <= 600
            ):
                if await uow.admin.setting("lifecycle_notifications", True) is True:
                    await uow.outbox.enqueue(
                        f"group:{result['event_id']}",
                        "message",
                        self.main_admin_id,
                        {
                            "text": f"📌 تغییر وضعیت گروه\n{change.title or 'گروه'}\nشناسه: {change.chat_id}\nرویداد: {result['kind']}\nعضویت: {result['is_active']} · ادمینی: {result['is_admin']}"
                        },
                    )
        if result and self.cache:
            await self.cache.put("group", change.chat_id, cached)
        if result:
            log.info(
                {
                    "event": "group_lifecycle",
                    "chat_id": change.chat_id,
                    "actor_id": change.actor_id,
                    "is_active": change.active,
                    "is_admin": change.admin,
                    "can_send": change.can_send,
                    **result,
                }
            )
        return result

    async def claim_link(self, user_id, chat_id, request_id, interval):
        async with self._uow_factory() as uow:
            reason, remaining = await uow.groups.claim_link(
                user_id, chat_id, request_id, interval, self.links_per_minute
            )
        if reason in {"accepted", "user_rate_limited"}:
            return remaining
        log.info(
            {
                "event": "group_link_rejected",
                "chat_id": chat_id,
                "reason": reason,
                "retry_after_seconds": remaining,
            }
        )
        if reason == "group_rate_limited":
            raise DownloadError(
                f"این گروه هر دقیقه {self.links_per_minute} لینک می‌تونه بفرسته؛ "
                f"{remaining} ثانیه دیگر دوباره امتحان کن.",
                code=reason,
            )
        raise DownloadError(
            "ربات در این گروه دسترسی ارسال ندارد؛ دسترسی گروه را بررسی کن.", code=reason
        )

    async def migrate(self, old_id, new_id, occurred_at, *, replaying=False):
        if (
            type(old_id) is not int
            or type(new_id) is not int
            or old_id >= 0
            or new_id >= 0
            or old_id == new_id
        ):
            raise ValueError("Invalid group migration identities")
        try:
            async with self._uow_factory() as uow:
                result = await uow.groups.migrate(old_id, new_id, occurred_at)
        except Exception as error:
            if (
                replaying
                or self.retry_queue is None
                or not (
                    isinstance(error, (OSError, TimeoutError))
                    or type(error).__module__.startswith("asyncpg")
                )
            ):
                raise
            change = GroupChange(
                old_id,
                "migrated",
                f"migration:{old_id}:{new_id}",
                occurred_at,
                "group",
                active=False,
                migrated_to=new_id,
            )
            if not await self.retry_queue.append(change):
                raise
            log.error(
                {"event": "group_migration_deferred", "chat_id": old_id, "new_chat_id": new_id}
            )
            return
        if self.cache:
            await self.cache.invalidate("group", old_id)
            await self.cache.invalidate("group", new_id)
        if result:
            log.info(
                {"event": "group_migrated", "chat_id": old_id, "new_chat_id": new_id, **result}
            )
