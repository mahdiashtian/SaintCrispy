"""Transactional lifecycle and admission policy for Telegram groups."""

import logging

from repositories.interfaces.group import GroupRepository
from schemas.group import GroupChange
from schemas.media import DownloadError

from .base import TransactionalService

log = logging.getLogger("activity")


class GroupService(TransactionalService):
    def __init__(self, repository: GroupRepository, uow_factory, links_per_minute=5):
        super().__init__(uow_factory)
        if not 1 <= links_per_minute <= 1000:
            raise ValueError("Group links per minute must be between 1 and 1000")
        self.repository = repository
        self.links_per_minute = links_per_minute

    async def get(self, chat_id):
        return await self.repository.get(chat_id)

    async def record(self, change: GroupChange):
        async with self._uow_factory() as uow:
            result = await uow.groups.record(change)
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

    async def migrate(self, old_id, new_id, occurred_at):
        if old_id >= 0 or new_id >= 0 or old_id == new_id:
            raise ValueError("Invalid group migration identities")
        async with self._uow_factory() as uow:
            result = await uow.groups.migrate(old_id, new_id, occurred_at)
        if result:
            log.info(
                {"event": "group_migrated", "chat_id": old_id, "new_chat_id": new_id, **result}
            )
