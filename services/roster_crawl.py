"""Telegram-free crawl bookkeeping; every page is one atomic SQL operation."""

import uuid


class RosterCrawlService:
    def __init__(self, repository, uow_factory, *, max_concurrent=1, lease_seconds=120):
        self.repository, self.uow_factory = repository, uow_factory
        self.max_concurrent, self.lease_seconds = max_concurrent, lease_seconds

    async def enqueue(self, chat_id):
        async with self.uow_factory() as uow:
            return await uow.roster_crawls.enqueue(chat_id)

    async def progress(self, chat_id):
        return await self.repository.get(chat_id)

    async def summary(self):
        return await self.repository.summary()

    async def recent(self):
        return await self.repository.recent()

    async def report_state(self, value=None):
        async with self.uow_factory() as uow:
            if value is not None:
                await uow.admin.set_setting("roster_progress_message", value)
            return await uow.admin.setting("roster_progress_message", {})

    async def claim(self):
        async with self.uow_factory() as uow:
            return await uow.roster_crawls.claim(
                uuid.uuid4().hex, self.lease_seconds, self.max_concurrent
            )

    async def fence(self):
        return await self.repository.fence()

    async def valid(self, crawl):
        return await self.repository.valid(crawl, crawl.locked_by)

    async def page(self, crawl, users, skipped, **values):
        unique = {user.user_id: user for user in users}
        users = [unique[identity] for identity in sorted(unique)]
        skipped = sorted(set(skipped) - set(unique))
        async with self.uow_factory() as uow:
            return await uow.roster_crawls.write_page(
                crawl, crawl.locked_by, users, skipped, lease_seconds=self.lease_seconds, **values
            )

    async def advance_pass(self, crawl):
        async with self.uow_factory() as uow:
            return await uow.roster_crawls.advance_pass(crawl, crawl.locked_by)

    async def pause(self, crawl, reason, seconds, **flags):
        async with self.uow_factory() as uow:
            return await uow.roster_crawls.pause(crawl, crawl.locked_by, reason, seconds, **flags)

    async def finish(self, crawl, reason, **flags):
        async with self.uow_factory() as uow:
            return await uow.roster_crawls.finish(crawl, crawl.locked_by, reason, **flags)
