"""One private async connection/transaction for a domain operation."""

import sys
from contextlib import AsyncExitStack

from .admin import AdminRepository
from .daily_stat import DailyStatsRepository
from .group import GroupRepository
from .media import PostgresMediaRepository
from .membership import MembershipRepository
from .outbox import OutboxRepository
from .roster_crawl import RosterCrawlRepository
from .workflow import WorkflowRepository


class PostgresUnitOfWork:
    def __init__(self, pool, bot_id: int):
        self.pool, self.bot_id = pool, bot_id
        self._stack = None

    async def __aenter__(self):
        if self._stack is not None:
            raise RuntimeError("A unit of work cannot be shared across requests")
        stack = self._stack = AsyncExitStack()
        try:
            connection = await stack.enter_async_context(self.pool.acquire())
            await stack.enter_async_context(connection.transaction())
            self.media = PostgresMediaRepository(connection, self.bot_id)
            self.workflow = WorkflowRepository(connection, self.bot_id)
            self.groups = GroupRepository(connection, self.bot_id)
            self.memberships = MembershipRepository(connection, self.bot_id)
            self.admin = AdminRepository(connection, self.bot_id)
            self.daily_stats = DailyStatsRepository(connection, self.bot_id)
            self.outbox = OutboxRepository(connection, self.bot_id)
            self.roster_crawls = RosterCrawlRepository(connection, self.bot_id)
        except BaseException:
            await stack.__aexit__(*sys.exc_info())
            self._stack = None
            raise
        return self

    async def __aexit__(self, *exc):
        try:
            return await self._stack.__aexit__(*exc)
        finally:
            self._stack = None
