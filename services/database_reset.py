"""Explicit root-only reset; quiesce writers, erase SQL, clear Redis and stop."""

import logging

from schemas.admin import RESET_DATABASE_ACTION
from schemas.media import DownloadError

log = logging.getLogger("activity")


class DatabaseReset:
    def __init__(self, admin, repository, redis, *, guard, seal, shutdown):
        self.admin, self.repository, self.redis = admin, repository, redis
        self.guard, self.seal, self.shutdown = guard, seal, shutdown
        self.stopped = False

    async def prepare(self, owner):
        await self.admin.authorize(owner, root=True)
        counts = await self.repository.counts()
        token = await self.admin.repository.request(owner, "confirm", RESET_DATABASE_ACTION, {})
        log.info(
            {"event": "database_reset_prepared", "actor_id": owner, "rows": sum(counts.values())}
        )
        return token, counts

    async def cancel(self, owner, token):
        await self.admin.authorize(owner, root=True)
        async with self.admin.uow_factory() as uow:
            request = await uow.admin.consume(owner, token)
            if (
                not request
                or request["kind"] != "confirm"
                or request["action"] != RESET_DATABASE_ACTION
            ):
                raise DownloadError("این تأیید معتبر نیست یا قبلاً استفاده شده است.")
        log.info({"event": "database_reset_cancelled", "actor_id": owner})

    def _seal(self):
        self.stopped = True
        self.seal()

    async def confirm(self, owner, token):
        await self.admin.authorize(owner, root=True)
        async with self.guard(), self.repository.exclusive():
            await self.admin.authorize(owner, root=True)
            await self.redis.verify(self.redis.required())
            counts = await self.repository.erase(
                owner, token, redis_required=self.redis.required(), seal=self._seal
            )
            await recover_reset(self.repository, self.redis)
        log.warning(
            {
                "event": "database_reset_completed",
                "actor_id": owner,
                "tables": len(counts),
                "rows": sum(counts.values()),
            }
        )
        return counts


async def recover_reset(repository, redis):
    pending = await repository.pending()
    if not pending:
        return
    async with repository.exclusive():
        for bot_id, token, manifest in pending:
            await redis.purge(manifest)
            await repository.finish(bot_id, token)
        log.warning({"event": "database_reset_redis_completed", "resets": len(pending)})
