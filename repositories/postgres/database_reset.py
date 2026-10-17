"""Whole-application reset with a durable Redis recovery manifest."""

import json
from contextlib import asynccontextmanager

from db.postgres.models import TABLES
from repositories.postgres.admin import AdminRepository
from schemas.admin import RESET_DATABASE_ACTION, RESET_DATABASE_PENDING
from schemas.media import DownloadError

RESET_LOCK = 739286411
TABLE_NAMES = tuple(table.name for table in TABLES)
QUOTED_TABLES = ",".join('"' + name + '"' for name in TABLE_NAMES)


class DatabaseResetRepository:
    def __init__(self, connection, bot_id):
        self.connection, self.bot_id = connection, bot_id

    async def join_runtime(self):
        # Held by the dedicated coordinator connection until its pool release.
        await self.connection.execute("SELECT pg_advisory_lock_shared($1::bigint)", RESET_LOCK)

    @asynccontextmanager
    async def exclusive(self):
        if not await self.connection.fetchval(
            "SELECT pg_try_advisory_lock($1::bigint)", RESET_LOCK
        ):
            raise DownloadError("ابتدا ربات‌های دیگرِ متصل به همین دیتابیس را متوقف کن.")
        try:
            # Older coordinators use a different bigint session lock, without the shared lease.
            if await self.connection.fetchval(
                """SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory'
                AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                AND pid<>pg_backend_pid() AND objsubid=1 AND granted)"""
            ):
                raise DownloadError("یک پردازش دیگر روی دیتابیس فعال است؛ ابتدا آن را متوقف کن.")
            yield
        finally:
            await self.connection.execute("SELECT pg_advisory_unlock($1::bigint)", RESET_LOCK)

    async def counts(self):
        rows = await self.connection.fetch(
            " UNION ALL ".join(
                f"SELECT '{name}' AS name,count(*) AS count FROM \"{name}\"" for name in TABLE_NAMES
            )
        )
        return {row["name"]: row["count"] for row in rows}

    async def erase(self, owner, token, *, redis_required, seal):
        async with self.connection.transaction():
            await self.connection.execute("SET LOCAL lock_timeout='5s'")
            # No CASCADE: an unknown external foreign key must abort rather than erase extra tables.
            await self.connection.execute(f"LOCK TABLE {QUOTED_TABLES} IN ACCESS EXCLUSIVE MODE")
            request = await AdminRepository(self.connection, self.bot_id).consume(owner, token)
            if (
                not request
                or request["kind"] != "confirm"
                or request["action"] != RESET_DATABASE_ACTION
            ):
                raise DownloadError("تأیید پاکسازی کامل منقضی یا قبلاً استفاده شده است.")
            counts = await self.counts()
            columns = await self.connection.fetch(
                """SELECT c.relname FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
                WHERE c.relnamespace=current_schema()::regnamespace AND c.relname=ANY($1::text[])
                AND a.attname='telegram_account_id' AND NOT a.attisdropped""",
                list(TABLE_NAMES),
            )
            names = [row["relname"] for row in columns]
            identities = await self.connection.fetch(
                " UNION ".join(f'SELECT telegram_account_id FROM "{name}"' for name in names)
            )
            manifest = {
                "version": 1,
                "bot_ids": sorted(
                    {self.bot_id, *(row["telegram_account_id"] for row in identities)}
                ),
                "redis_required": redis_required,
            }
            seal()
            await self.connection.execute(f"TRUNCATE {QUOTED_TABLES} RESTART IDENTITY")
            # Kept only until Redis cleanup succeeds. It fences replay after a crash or outage.
            await self.connection.execute(
                """INSERT INTO admin_requests
                (telegram_account_id,token,owner_id,kind,action,payload,consumed_at,expires_at)
                VALUES($1,$2,$3,'confirm',$4,$5::jsonb,clock_timestamp(),'infinity')""",
                self.bot_id,
                token,
                owner,
                RESET_DATABASE_PENDING,
                json.dumps(manifest),
            )
        return counts

    async def pending(self):
        rows = await self.connection.fetch(
            "SELECT telegram_account_id,token,payload FROM admin_requests WHERE action=$1 AND consumed_at IS NOT NULL",
            RESET_DATABASE_PENDING,
        )
        return tuple(
            (row["telegram_account_id"], row["token"], json.loads(row["payload"])) for row in rows
        )

    async def finish(self, bot_id, token):
        await self.connection.execute(
            "DELETE FROM admin_requests WHERE telegram_account_id=$1 AND token=$2 AND action=$3",
            bot_id,
            token,
            RESET_DATABASE_PENDING,
        )
