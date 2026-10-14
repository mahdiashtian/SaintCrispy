"""Connection ownership and additive, transactional startup migrations."""

from contextlib import asynccontextmanager

import asyncpg

from .migrations import MIGRATIONS as MIGRATIONS
from .models import WORKFLOW_SCHEMA as WORKFLOW_SCHEMA


@asynccontextmanager
async def create_pool(url: str, max_size: int = 8):
    async with await asyncpg.create_pool(
        url, min_size=1, max_size=max_size, command_timeout=30
    ) as pool:
        yield pool


class Database:
    """Own the bounded native async pool; each unit of work acquires privately."""

    def __init__(self, url: str, pool_size: int = 32):
        self._context = create_pool(url, pool_size)
        self.pool = None

    async def __aenter__(self):
        self.pool = await self._context.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._context.__aexit__(*exc)


async def migrate(pool) -> None:
    # Serialize DDL across bots sharing the same database, without deleting data.
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(739286410)")
        await connection.execute("""CREATE TABLE IF NOT EXISTS app_schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        applied = await connection.fetch("SELECT version FROM app_schema_migrations")
        versions = {row["version"] for row in applied}
        for version, name, sql in MIGRATIONS:
            if version not in versions:
                await connection.execute(sql)
                await connection.execute(
                    "INSERT INTO app_schema_migrations (version,name) VALUES ($1,$2)", version, name
                )
