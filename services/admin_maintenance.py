"""Account-scoped logical backups and bounded, confirmed maintenance.

Unlike executable PostgreSQL dumps received through Telegram, this format can
only insert validated records into the listed public-data tables. Restoration
is additive: existing records and live jobs are never replaced or replayed.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from schemas.admin import AdminDataFile
from schemas.media import DownloadError

TABLES = (
    "bot_users",
    "bot_groups",
    "group_memberships",
    "membership_events",
    "group_events",
    "user_media_history",
)
EXCLUDED = (
    "credentials and site accounts",
    "Telegram peers and file references",
    "signed download metadata",
    "pending jobs, requests and conversations",
    "raw link history and operational logs",
)
MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50000


@asynccontextmanager
async def default_guard():
    yield


class AdminMaintenance:
    def __init__(self, admin):
        self.admin = admin
        self.guard = default_guard
        self.cpu_slots = asyncio.Semaphore(2)

    async def columns(self, connection, table):
        if table not in TABLES:
            raise ValueError("Unknown backup table")
        rows = await connection.fetch(
            """SELECT a.attname name,t.typname type,a.attnotnull required
            FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid
            WHERE a.attrelid=to_regclass($1) AND a.attnum>0 AND NOT a.attisdropped
            AND a.attidentity='' ORDER BY a.attnum""",
            table,
        )
        return tuple(dict(row) for row in rows)

    async def backup(self, owner):
        await self.admin.authorize(owner, root=True)
        tables, count = {}, 0
        async with self.admin.uow_factory() as uow:
            connection = uow.admin.connection
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            for table in TABLES:
                columns = await self.columns(connection, table)
                names = ",".join('"' + row["name"] + '"' for row in columns)
                rows = await connection.fetch(
                    f"SELECT {names} FROM {table} WHERE telegram_account_id=$1 LIMIT $2",
                    self.admin.repository.bot_id,
                    MAX_ROWS - count + 1,
                )
                count += len(rows)
                if count > MAX_ROWS:
                    raise DownloadError(
                        "حجم داده‌ها بیشتر از سقف پنل است؛ بک‌آپ کامل را با pg_dump روی سرور بگیر."
                    )
                async with self.cpu_slots:
                    tables[table] = await asyncio.to_thread(encode_rows, rows, columns)
            await uow.admin.audit(owner, "backup_public_data", details={"rows": count})
        payload = {
            "format": "saintcrispy-public-data",
            "version": 1,
            "account_id": self.admin.repository.bot_id,
            "created_at": datetime.now(UTC).isoformat(),
            "tables": tables,
            "excluded": list(EXCLUDED),
        }
        async with self.cpu_slots:
            content = await asyncio.to_thread(serialize_payload, payload)
        if len(content) > MAX_BYTES:
            raise DownloadError(
                "حجم بک‌آپ بیشتر از سقف ۲۰ مگابایت پنل است؛ از pg_dump روی سرور استفاده کن."
            )
        return AdminDataFile("saintcrispy-public-data.json", content, None)

    async def prepare_restore(self, owner, content):
        await self.admin.authorize(owner, root=True)
        payload = await self.validate(content)
        async with self.admin.uow_factory() as uow:
            token = await uow.admin.request(owner, "confirm", "restore_public_data", payload)
            await uow.admin.audit(
                owner,
                "prepare_restore",
                details={"rows": sum(map(len, payload["tables"].values()))},
            )
        return token, {table: len(rows) for table, rows in payload["tables"].items()}

    async def validate(self, content):
        try:
            if not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES:
                raise ValueError("Backup size")
            async with self.cpu_slots:
                payload = await asyncio.to_thread(
                    json.loads,
                    content,
                    object_pairs_hook=unique_object,
                    parse_constant=reject_constant,
                )
                async with self.admin.uow_factory() as uow:
                    metadata = {
                        table: await self.columns(uow.admin.connection, table) for table in TABLES
                    }
                return await asyncio.to_thread(
                    validate_payload, payload, metadata, self.admin.repository.bot_id
                )
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
            raise DownloadError(
                "فایل بک‌آپ معتبر و کاملِ داده‌های عمومی همین ربات را بفرست؛ SQL و pg_dump از تلگرام اجرا نمی‌شوند."
            ) from error

    async def confirm_restore(self, owner, token):
        await self.admin.authorize(owner, root=True)
        counts = {}
        async with self.guard():
            async with self.admin.uow_factory() as uow:
                await self.admin.authorize(owner, root=True, repository=uow.admin)
                request = await uow.admin.consume(owner, token)
                if (
                    not request
                    or request["kind"] != "confirm"
                    or request["action"] != "restore_public_data"
                ):
                    raise DownloadError("تأیید بازیابی منقضی یا قبلاً استفاده شده است.")
                tables = request["payload"]["tables"]
                for table in TABLES:
                    columns = await self.columns(uow.admin.connection, table)
                    names = ",".join('"' + row["name"] + '"' for row in columns)
                    parameters = ",".join(f"${index}" for index in range(1, len(columns) + 1))
                    async with self.cpu_slots:
                        values = await asyncio.to_thread(
                            restore_values,
                            tables[table],
                            columns,
                            self.admin.repository.bot_id,
                            table,
                        )
                    if values:
                        insertion = f"INSERT INTO {table} ({names}) VALUES ({parameters}) ON CONFLICT DO NOTHING"
                        if table in {"group_memberships", "membership_events"}:
                            positions = {
                                column["name"]: index for index, column in enumerate(columns, 1)
                            }
                            account, chat, user = (
                                f"${positions[key]}"
                                for key in ("telegram_account_id", "chat_id", "user_id")
                            )
                            condition = (
                                f"NOT EXISTS (SELECT 1 FROM membership_departures WHERE telegram_account_id={account} AND chat_id={chat} AND user_id={user})"
                                if table == "group_memberships"
                                else f"EXISTS (SELECT 1 FROM group_memberships WHERE telegram_account_id={account} AND chat_id={chat} AND user_id={user})"
                            )
                            insertion = f"INSERT INTO {table} ({names}) SELECT {parameters} WHERE {condition} ON CONFLICT DO NOTHING"
                        for start in range(0, len(values), 1000):
                            await uow.admin.connection.executemany(
                                insertion, values[start : start + 1000]
                            )
                    counts[table] = len(values)
                await uow.admin.audit(
                    owner,
                    "restore_public_data",
                    details={"submitted_rows": sum(counts.values()), "mode": "insert_missing_only"},
                )
            await self.invalidate(tables)
        return counts

    async def invalidate(self, tables):
        if not self.admin.cache:
            return
        for table, kind, key in (
            ("bot_users", "user", "user_id"),
            ("bot_groups", "group", "chat_id"),
        ):
            rows = tables.get(table, [])
            for start in range(0, len(rows), 16):
                await asyncio.gather(
                    *(
                        self.admin.cache.invalidate(kind, row[key])
                        for row in rows[start : start + 16]
                    )
                )

    async def cleanup_preview(self, owner, entity, filters):
        await self.admin.authorize(owner, root=True)
        where, params, table, key = cleanup_query(entity, filters, self.admin.main_admin_id)
        connection = self.admin.repository.connection
        count = await connection.fetchval(
            f"SELECT count(*) FROM {table} t WHERE telegram_account_id=$1 AND ({where})",
            self.admin.repository.bot_id,
            *params,
        )
        return {"matched": count, "batch": min(1000, count), "entity": entity}

    async def prepare_cleanup(self, owner, entity, filters):
        preview = await self.cleanup_preview(owner, entity, filters)
        if not preview["matched"]:
            raise DownloadError("رکوردی با این فیلتر پیدا نشد.")
        token = await self.admin.repository.request(
            owner, "confirm", "cleanup_public_data", {"entity": entity, "filters": filters}
        )
        return token, preview

    async def confirm_cleanup(self, owner, token):
        await self.admin.authorize(owner, root=True)
        async with self.guard():
            async with self.admin.uow_factory() as uow:
                await self.admin.authorize(owner, root=True, repository=uow.admin)
                request = await uow.admin.consume(owner, token)
                if (
                    not request
                    or request["kind"] != "confirm"
                    or request["action"] != "cleanup_public_data"
                ):
                    raise DownloadError("تأیید پاکسازی منقضی یا قبلاً استفاده شده است.")
                entity, filters = request["payload"]["entity"], request["payload"]["filters"]
                where, params, table, key = cleanup_query(entity, filters, self.admin.main_admin_id)
                ids = await uow.admin.connection.fetch(
                    f"SELECT {key} FROM {table} t WHERE telegram_account_id=$1 AND ({where}) ORDER BY {key} LIMIT 1000 FOR UPDATE SKIP LOCKED",
                    self.admin.repository.bot_id,
                    *params,
                )
                identities = [row[key] for row in ids]
                if identities:
                    if entity == "groups":
                        await uow.admin.connection.execute(
                            "SELECT pg_advisory_xact_lock($1,$2)",
                            self.admin.repository.bot_id % 2147483647,
                            204217,
                        )
                        config = await uow.admin.setting("channel_management", {})
                        if isinstance(config, dict) and isinstance(config.get("watches"), list):
                            remaining = [
                                row
                                for row in config["watches"]
                                if row.get("chat_id") not in identities
                            ]
                            if len(remaining) != len(config["watches"]):
                                await uow.admin.set_setting(
                                    "channel_management", {**config, "watches": remaining}
                                )
                        for child in ("group_events", "group_request_limits"):
                            await uow.admin.connection.execute(
                                f"DELETE FROM {child} WHERE telegram_account_id=$1 AND chat_id=ANY($2::bigint[])",
                                self.admin.repository.bot_id,
                                identities,
                            )
                    await uow.admin.connection.execute(
                        f"DELETE FROM {table} t WHERE telegram_account_id=$1 AND {key}=ANY($2::bigint[])",
                        self.admin.repository.bot_id,
                        identities,
                    )
                await uow.admin.audit(
                    owner,
                    "cleanup_public_data",
                    details={"entity": entity, "count": len(identities), "filters": filters},
                )
            if entity == "groups" and identities:
                self.admin.channels.invalidate()
            if self.admin.cache:
                for identity in identities:
                    await self.admin.cache.invalidate(
                        "user" if entity == "users" else "group", identity
                    )
        return len(identities)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_date(value):
    if not isinstance(value, str):
        raise ValueError("Invalid datetime")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Naive datetime")
    return result


def encode_row(row, columns):
    return {
        column["name"]: row[column["name"]].isoformat()
        if isinstance(row[column["name"]], datetime)
        else json.loads(row[column["name"]])
        if column["type"] in {"json", "jsonb"} and isinstance(row[column["name"]], str)
        else row[column["name"]]
        for column in columns
    }


def decode_row(row, columns, account):
    if (
        not isinstance(row, dict)
        or set(row) != {column["name"] for column in columns}
        or type(row.get("telegram_account_id")) is not int
        or row["telegram_account_id"] != account
    ):
        raise ValueError("Invalid row shape or account")
    result = {}
    for column in columns:
        name, kind = column["name"], column["type"]
        value = row[name]
        if value is None:
            if column["required"]:
                raise ValueError("Missing required value")
        elif kind in {"int8", "int4", "int2"}:
            bits = {"int8": 63, "int4": 31, "int2": 15}[kind]
            if type(value) is not int or not -(2**bits) <= value < 2**bits:
                raise ValueError("Invalid integer")
        elif kind == "bool":
            if type(value) is not bool:
                raise ValueError("Invalid boolean")
        elif kind in {"text", "varchar"}:
            if not isinstance(value, str) or len(value) > 10000 or "\x00" in value:
                raise ValueError("Invalid string")
            value.encode("utf-8")
        elif kind == "timestamptz":
            value = parse_date(value)
        elif kind in {"json", "jsonb"}:
            value = json.dumps(value, allow_nan=False)
        else:
            raise ValueError("Unsupported backup type")
        result[name] = value
    return result


def cleanup_query(entity, filters, root_id):
    if not isinstance(filters, dict):
        raise DownloadError("فیلتر پاکسازی معتبر نیست.")
    if entity == "users":
        if (
            set(filters) != {"started", "membership", "kind"}
            or filters["started"] not in {"all", "yes", "no"}
            or filters["membership"] not in {"all", "yes", "no"}
            or filters["kind"] not in {"all", "real", "unknown", "bot", "deleted", "invalid"}
        ):
            raise DownloadError("فیلتر کاربران معتبر نیست.")
        where = [
            "user_id<>$2",
            "NOT is_admin",
            "NOT EXISTS (SELECT 1 FROM group_memberships m JOIN bot_groups g USING(telegram_account_id,chat_id) WHERE m.telegram_account_id=t.telegram_account_id AND m.user_id=t.user_id AND m.is_active AND g.is_active IS TRUE)",
        ]
        if filters["started"] != "all":
            where.append("is_started IS " + ("TRUE" if filters["started"] == "yes" else "FALSE"))
        if filters["membership"] != "all":
            where.append(
                ("" if filters["membership"] == "yes" else "NOT ")
                + "EXISTS (SELECT 1 FROM group_memberships m WHERE m.telegram_account_id=t.telegram_account_id AND m.user_id=t.user_id)"
            )
        if filters["kind"] != "all":
            where.append(
                "account_kind IN ('bot','deleted')"
                if filters["kind"] == "invalid"
                else "account_kind='" + filters["kind"] + "'"
            )
        return " AND ".join(where), [root_id or 0], "bot_users", "user_id"
    if entity == "groups":
        if set(filters) != {"admin", "members", "owner"} or any(
            value not in {"all", "yes", "no"} for value in filters.values()
        ):
            raise DownloadError("فیلتر گروه‌ها معتبر نیست.")
        where = [
            "is_active IS FALSE",
            "migrated_to IS NULL",
            "NOT EXISTS (SELECT 1 FROM bot_groups origin WHERE origin.telegram_account_id=t.telegram_account_id AND origin.migrated_to=t.chat_id)",
        ]
        for key, expression in (
            ("admin", "is_admin IS TRUE"),
            (
                "members",
                "EXISTS (SELECT 1 FROM group_memberships m WHERE m.telegram_account_id=t.telegram_account_id AND m.chat_id=t.chat_id AND m.is_active)",
            ),
            ("owner", "owner_id IS NOT NULL"),
        ):
            if filters[key] != "all":
                where.append("(" + ("" if filters[key] == "yes" else "NOT ") + expression + ")")
        return " AND ".join(where), [], "bot_groups", "chat_id"
    raise DownloadError("نوع داده برای پاکسازی معتبر نیست.")


def validate_payload(payload, metadata, account):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"format", "version", "account_id", "created_at", "tables", "excluded"}
        or payload["format"] != "saintcrispy-public-data"
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or type(payload["account_id"]) is not int
        or payload["account_id"] != account
        or payload["excluded"] != list(EXCLUDED)
    ):
        raise ValueError("Backup header")
    parse_date(payload["created_at"])
    tables = payload["tables"]
    if (
        not isinstance(tables, dict)
        or set(tables) != set(TABLES)
        or any(not isinstance(rows, list) for rows in tables.values())
        or sum(map(len, tables.values())) > MAX_ROWS
    ):
        raise ValueError("Backup tables")
    for table, rows in tables.items():
        for row in rows:
            decode_row(row, metadata[table], account)
    users = {row["user_id"] for row in tables["bot_users"]}
    groups = {row["chat_id"] for row in tables["bot_groups"]}
    memberships = {(row["chat_id"], row["user_id"]) for row in tables["group_memberships"]}
    if (
        len(users) != len(tables["bot_users"])
        or len(groups) != len(tables["bot_groups"])
        or len(memberships) != len(tables["group_memberships"])
    ):
        raise ValueError("Duplicate primary keys")
    if (
        any(chat not in groups or user not in users for chat, user in memberships)
        or any(
            (row["chat_id"], row["user_id"]) not in memberships
            for row in tables["membership_events"]
        )
        or any(row["chat_id"] not in groups for row in tables["group_events"])
    ):
        raise ValueError("Missing backup parent")
    keys = {
        "membership_events": ("chat_id", "event_key"),
        "group_events": ("chat_id", "event_key"),
        "user_media_history": ("user_id", "site", "content_id", "quality"),
    }
    for table, fields in keys.items():
        if len({tuple(row[field] for field in fields) for row in tables[table]}) != len(
            tables[table]
        ):
            raise ValueError("Duplicate backup identity")
    for row in tables["bot_users"]:
        if row["user_id"] <= 0 or row["account_kind"] not in {"unknown", "real", "bot", "deleted"}:
            raise ValueError("Invalid user identity")
        for name in ("username", "first_name", "last_name"):
            bounded_text(row.get(name), 256, nullable=True)
    for row in tables["bot_groups"]:
        if (
            row["chat_id"] >= 0
            or row["chat_kind"] not in {"group", "supergroup"}
            or (row["admin_since"] is not None and row["is_admin"] is not True)
            or (row["links_per_minute"] is not None and not 1 <= row["links_per_minute"] <= 1000)
        ):
            raise ValueError("Invalid group standing")
        for name in ("title", "username", "link"):
            bounded_text(row.get(name), 512, nullable=True)
        admins, cursors = row["admin_ids"], row["event_cursors"]
        if (
            not isinstance(admins, list)
            or len(admins) > 200
            or any(type(identity) is not int or not 0 < identity < 2**63 for identity in admins)
        ):
            raise ValueError("Invalid administrator snapshot")
        if not isinstance(cursors, dict) or len(cursors) > 1000:
            raise ValueError("Invalid group cursors")
        for stream, sequence in cursors.items():
            # GroupChange deliberately permits named stream extensions, such as
            # probe/observation. Preserve that contract while rejecting values
            # that would break the next sequence comparison.
            bounded_text(stream, 80)
            if type(sequence) is not int or not 0 <= sequence < 2**63:
                raise ValueError("Invalid group cursor sequence")
    for row in tables["group_memberships"]:
        if (
            row["message_count"] < 0
            or row["state_event_order"] < 0
            or row["state_event_stream"]
            not in {"message", "basic", "participant", "service", "roster", "departure"}
        ):
            raise ValueError("Invalid membership count")
    for row in tables["membership_events"]:
        bounded_text(row["event_key"], 180)
    for row in tables["group_events"]:
        bounded_text(row["event_key"], 256)
        bounded_text(row["kind"], 80)
    if any(
        row["kind"] not in {"message", "joined", "left"} for row in tables["membership_events"]
    ) or any(row["delivery_count"] < 0 for row in tables["user_media_history"]):
        raise ValueError("Invalid event or delivery count")
    return payload


def bounded_text(value, maximum, *, nullable=False):
    if value is None and nullable:
        return
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not nullable and not value)
        or "\x00" in value
    ):
        raise ValueError("Invalid backup text")
    value.encode("utf-8")


def reject_constant(value):
    raise ValueError("Non-finite JSON")


def encode_rows(rows, columns):
    return [encode_row(dict(row), columns) for row in rows]


def restore_values(rows, columns, account, table):
    values = []
    for row in rows:
        decoded = decode_row(row, columns, account)
        if table == "bot_users":
            decoded["is_admin"] = False
        if table == "group_memberships" and "state_revision" in decoded:
            decoded["state_revision"] = 0
        values.append(tuple(decoded[column["name"]] for column in columns))
    return values


def serialize_payload(payload):
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
