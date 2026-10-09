"""Connection ownership and additive, transactional startup migrations."""

from contextlib import asynccontextmanager

import asyncpg

from .schema import SCHEMA

WORKFLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_users (
    telegram_account_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    is_started BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, user_id)
);
CREATE TABLE IF NOT EXISTS bot_conversations (
    telegram_account_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    state TEXT NOT NULL,
    data JSONB NOT NULL DEFAULT '{}',
    revision BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, user_id, chat_id)
);
CREATE TABLE IF NOT EXISTS download_menus (
    telegram_account_id BIGINT NOT NULL,
    token TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id INTEGER,
    metadata JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, token)
);
CREATE INDEX IF NOT EXISTS download_menus_owner
    ON download_menus (telegram_account_id, user_id, chat_id, created_at);
"""
MIGRATIONS = ((1, "existing_media_schema", SCHEMA), (2, "durable_bot_workflow", WORKFLOW_SCHEMA))


@asynccontextmanager
async def create_pool(url: str):
    async with await asyncpg.create_pool(url, min_size=1, max_size=8, command_timeout=30) as pool:
        yield pool


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
