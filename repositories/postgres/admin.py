"""SQL administration reads, audited writes and scoped confirmation tokens."""

import asyncio
import json
import secrets

from schemas.admin import AdminJob, AdminUser

USER_FIELDS = (
    "user_id,username,first_name,is_started,is_admin,is_banned,is_blocked,account_kind,last_seen_at"
)
JOB_FIELDS = (
    "id,kind,status,requested_by,total_count,success_count,failure_count,skipped_count,created_at"
)


class AdminRepository:
    def __init__(self, connection, bot_id):
        self.connection, self.bot_id = connection, bot_id

    async def authorized(self, user_id):
        return bool(
            await self.connection.fetchval(
                "SELECT is_admin AND NOT is_banned FROM bot_users WHERE telegram_account_id=$1 AND user_id=$2",
                self.bot_id,
                user_id,
            )
        )

    async def banned(self, user_id):
        return bool(
            await self.connection.fetchval(
                "SELECT is_banned FROM bot_users WHERE telegram_account_id=$1 AND user_id=$2",
                self.bot_id,
                user_id,
            )
        )

    async def setting(self, key, default=None):
        value = await self.connection.fetchval(
            "SELECT value FROM app_settings WHERE telegram_account_id=$1 AND key=$2",
            self.bot_id,
            key,
        )
        decoded = json.loads(value) if value is not None else default
        if key == "channel_management":
            from repositories.postgres.channels import ChannelRepository

            return {
                **(decoded if isinstance(decoded, dict) else {}),
                **await ChannelRepository(self.connection, self.bot_id).get(),
            }
        return decoded

    async def set_setting(self, key, value):
        if key == "channel_management":
            from repositories.postgres.channels import ChannelRepository

            await ChannelRepository(self.connection, self.bot_id).replace(value)
            value = {
                k: v
                for k, v in value.items()
                if k not in {"forced_channels", "archive", "storage", "watches"}
            }
        await self.connection.execute(
            """INSERT INTO app_settings (telegram_account_id,key,value) VALUES ($1,$2,$3::jsonb)
            ON CONFLICT (telegram_account_id,key) DO UPDATE SET value=EXCLUDED.value,
            revision=app_settings.revision+1,updated_at=now()""",
            self.bot_id,
            key,
            json.dumps(value),
        )

    async def audit(self, actor, action, target=None, details=None):
        await self.connection.execute(
            "INSERT INTO admin_actions (telegram_account_id,actor_id,action,target_id,details) VALUES ($1,$2,$3,$4,$5::jsonb)",
            self.bot_id,
            actor,
            action,
            target,
            json.dumps(details or {}),
        )

    async def users(self, cursor=0, limit=20):
        rows = await self.connection.fetch(
            f"SELECT {USER_FIELDS} FROM bot_users WHERE telegram_account_id=$1 AND user_id>$2 ORDER BY user_id LIMIT $3",
            self.bot_id,
            cursor,
            limit,
        )
        return tuple(AdminUser(**dict(row)) for row in rows)

    async def administrators(self):
        rows = await self.connection.fetch(
            f"SELECT {USER_FIELDS} FROM bot_users WHERE telegram_account_id=$1 AND is_admin ORDER BY user_id LIMIT 500",
            self.bot_id,
        )
        return tuple(AdminUser(**dict(row)) for row in rows)

    async def group_directory(self, page=0, active=None, order="date"):
        from schemas.group import GroupSnapshot

        if type(page) is not int or not 0 <= page <= 1000000 or active not in (None, True, False):
            raise ValueError("Invalid directory page")
        ordering = {
            "date": "added_at DESC NULLS LAST,chat_id",
            "members": "member_count DESC NULLS LAST,chat_id",
            "members_asc": "member_count ASC NULLS LAST,chat_id",
            "activity": "last_activity_at DESC NULLS LAST,chat_id",
        }.get(order)
        if ordering is None:
            raise ValueError("Invalid directory order")
        where = "telegram_account_id=$1 AND ($2::boolean IS NULL OR is_active=$2)"
        total = await self.connection.fetchval(
            f"SELECT count(*) FROM bot_groups WHERE {where}", self.bot_id, active
        )
        page = min(page, max(0, (total - 1) // 10))
        rows = await self.connection.fetch(
            f"SELECT chat_id,title,username,is_active,is_admin,can_send,added_by_user_id,added_at,readded_at,removed_at,admin_since,last_seen_at,last_event_at,migrated_to,chat_kind,owner_id,admin_ids,member_count,link,last_activity_at,links_per_minute,revision FROM bot_groups WHERE {where} ORDER BY {ordering} LIMIT 10 OFFSET $3",
            self.bot_id,
            active,
            page * 10,
        )
        groups = []
        for row in rows:
            value = dict(row)
            value["admin_ids"] = tuple(json.loads(value["admin_ids"]))
            groups.append(GroupSnapshot(**value))
        return tuple(groups), total, page

    async def user(self, query):
        if type(query) is int:
            row = await self.connection.fetchrow(
                f"SELECT {USER_FIELDS} FROM bot_users WHERE telegram_account_id=$1 AND user_id=$2",
                self.bot_id,
                query,
            )
        else:
            row = await self.connection.fetchrow(
                f"SELECT {USER_FIELDS} FROM bot_users WHERE telegram_account_id=$1 AND lower(username)=lower($2) ORDER BY last_seen_at DESC LIMIT 1",
                self.bot_id,
                query.removeprefix("@"),
            )
        return AdminUser(**dict(row)) if row else None

    async def user_trace(self, user_id, *, page=0, limit=10):
        rows = await self.connection.fetch(
            """SELECT site,content_id,quality,delivery_count,last_method,last_delivered_at
            FROM user_media_history WHERE telegram_account_id=$1 AND user_id=$2
            ORDER BY last_delivered_at DESC,site,content_id,quality LIMIT $3 OFFSET $4""",
            self.bot_id,
            user_id,
            limit,
            page * 10,
        )
        return tuple(dict(row) for row in rows)

    async def public_trace(self, user_id):
        profile = await self.connection.fetchrow(
            "SELECT user_id,username,first_name,is_started,is_admin,is_banned,is_blocked,account_kind,first_seen_at,started_at,last_seen_at,last_active_at,start_source,start_chat_id FROM bot_users WHERE telegram_account_id=$1 AND user_id=$2",
            self.bot_id,
            user_id,
        )
        rows = await self.connection.fetch(
            """SELECT m.chat_id,g.title,m.is_active,m.last_seen_at,m.message_count,
            count(*) OVER() AS total_groups,count(*) FILTER(WHERE m.is_active) OVER() AS active_groups
            FROM group_memberships m JOIN bot_groups g USING(telegram_account_id,chat_id)
            WHERE m.telegram_account_id=$1 AND m.user_id=$2
            ORDER BY m.is_active DESC,m.last_seen_at DESC,m.chat_id LIMIT 25""",
            self.bot_id,
            user_id,
        )
        return {
            "profile": dict(profile) if profile else None,
            "groups": [dict(row) for row in rows],
            **await self.download_page(user_id),
        }

    async def download_page(self, user_id, page=0):
        rows = await self.user_trace(user_id, page=page, limit=11)
        return {"downloads": rows[:10], "download_page": page, "has_next": len(rows) > 10}

    async def members(self, chat_id, cursor=0, limit=20):
        rows = await self.connection.fetch(
            """SELECT m.user_id,u.username,u.first_name,m.is_active,m.last_seen_at,m.message_count
            FROM group_memberships m JOIN bot_users u USING (telegram_account_id,user_id)
            WHERE m.telegram_account_id=$1 AND m.chat_id=$2 AND m.user_id>$3
            ORDER BY m.user_id LIMIT $4""",
            self.bot_id,
            chat_id,
            cursor,
            limit,
        )
        return tuple(dict(row) for row in rows)

    async def member_directory(self, chat_id, page=0, active="all", started="all"):
        if (
            type(page) is not int
            or not 0 <= page <= 1000000
            or active not in {"all", "in", "out"}
            or started not in {"all", "yes", "no"}
        ):
            raise ValueError("Invalid membership page")
        where = "m.telegram_account_id=$1 AND m.chat_id=$2 AND ($3::boolean IS NULL OR m.is_active=$3) AND ($4::boolean IS NULL OR u.is_started=$4)"
        params = (
            self.bot_id,
            chat_id,
            {"all": None, "in": True, "out": False}[active],
            {"all": None, "yes": True, "no": False}[started],
        )
        total = await self.connection.fetchval(
            f"SELECT count(*) FROM group_memberships m JOIN bot_users u USING (telegram_account_id,user_id) WHERE {where}",
            *params,
        )
        page = min(page, max(0, (total - 1) // 10))
        rows = await self.connection.fetch(
            f"SELECT m.user_id,u.username,u.first_name,u.is_started,m.is_active,m.last_seen_at,m.message_count FROM group_memberships m JOIN bot_users u USING (telegram_account_id,user_id) WHERE {where} ORDER BY m.user_id LIMIT 10 OFFSET $5",
            *params,
            page * 10,
        )
        return tuple(dict(row) for row in rows), total, page

    async def clear_prompts(self, owner):
        await self.connection.execute(
            "UPDATE admin_requests SET consumed_at=clock_timestamp() WHERE telegram_account_id=$1 AND owner_id=$2 AND consumed_at IS NULL",
            self.bot_id,
            owner,
        )

    async def stats(self):
        row = await self.connection.fetchrow(
            """SELECT
            (SELECT count(*) FROM bot_users WHERE telegram_account_id=$1) users,
            (SELECT count(*) FROM bot_users WHERE telegram_account_id=$1 AND is_started) started,
            (SELECT count(*) FROM bot_groups WHERE telegram_account_id=$1 AND is_active) active_groups,
            (SELECT count(*) FROM bot_groups WHERE telegram_account_id=$1 AND is_active AND is_admin) admin_groups,
            (SELECT count(*) FROM bot_groups WHERE telegram_account_id=$1 AND is_active IS FALSE AND migrated_to IS NULL) removed_groups,
            (SELECT count(*) FROM bot_groups WHERE telegram_account_id=$1 AND is_active IS NULL) unknown_groups,
            (SELECT count(*) FROM group_memberships WHERE telegram_account_id=$1 AND is_active) members,
            (SELECT COALESCE(sum(message_count),0)::bigint FROM group_memberships WHERE telegram_account_id=$1) messages,
            (SELECT COALESCE(sum(view_count),0)::bigint FROM user_link_history WHERE telegram_account_id=$1) views,
            (SELECT COALESCE(sum(delivery_count),0)::bigint FROM user_media_history WHERE telegram_account_id=$1) deliveries,
            (SELECT count(*) FROM outbox_events WHERE telegram_account_id=$1 AND status IN ('pending','leased')) pending,
            (SELECT count(*) FROM outbox_events WHERE telegram_account_id=$1 AND status='dead') dead""",
            self.bot_id,
        )
        return dict(row)

    async def daily(self):
        rows = await self.connection.fetch(
            """SELECT (occurred_at AT TIME ZONE 'Asia/Tehran')::date day,
            count(DISTINCT chat_id) FILTER (WHERE is_admin IS TRUE AND previous_admin IS DISTINCT FROM TRUE) admin_gained,
            count(DISTINCT chat_id) FILTER (WHERE is_admin IS FALSE AND previous_admin IS TRUE) admin_lost,
            count(DISTINCT chat_id) FILTER (WHERE is_active IS TRUE AND previous_active IS DISTINCT FROM TRUE) joined,
            count(DISTINCT chat_id) FILTER (WHERE is_active IS FALSE AND previous_active IS TRUE) removed
            FROM group_events WHERE telegram_account_id=$1 AND applied
            AND occurred_at>=now()-interval '7 days' GROUP BY day ORDER BY day DESC""",
            self.bot_id,
        )
        return tuple(dict(row) for row in rows)

    async def daily_summary(self, day):
        from repositories.postgres.daily_stat import DailyStatsRepository

        return await DailyStatsRepository(self.connection, self.bot_id).snapshot(day)

    async def stats_history(self, limit=14, offset=0):
        from repositories.postgres.daily_stat import DailyStatsRepository

        return await DailyStatsRepository(self.connection, self.bot_id).history(limit, offset)

    async def history_count(self):
        return await self.connection.fetchval(
            "SELECT count(*) FROM daily_stats WHERE telegram_account_id=$1", self.bot_id
        )

    async def change_user(self, user_id, field, value):
        if field not in {"is_admin", "is_banned"} or type(value) is not bool:
            raise ValueError("Invalid administrator write")
        # Definitive invalid-account evidence needs explicit revalidation before a role edit.
        if await self.connection.fetchval(
            "SELECT 1 FROM user_account_tombstones WHERE telegram_account_id=$1 AND user_id=$2",
            self.bot_id,
            user_id,
        ):
            raise ValueError("Telegram account was verified as deleted or a bot")
        await self.connection.execute(
            f"INSERT INTO bot_users (telegram_account_id,user_id,{field}) VALUES ($1,$2,$3) ON CONFLICT (telegram_account_id,user_id) DO UPDATE SET {field}=EXCLUDED.{field}",
            self.bot_id,
            user_id,
            value,
        )
        if await self.connection.fetchval(
            "SELECT 1 FROM user_account_tombstones WHERE telegram_account_id=$1 AND user_id=$2",
            self.bot_id,
            user_id,
        ):
            raise ValueError("Telegram account was verified as deleted or a bot")

    async def group_limit(self, chat_id, value):
        return await self.connection.fetchval(
            "UPDATE bot_groups SET links_per_minute=$3,updated_at=now(),revision=revision+1 WHERE telegram_account_id=$1 AND chat_id=$2 RETURNING chat_id",
            self.bot_id,
            chat_id,
            value,
        )

    async def request(self, owner, kind, action, payload):
        token = secrets.token_hex(8)
        encoded = (
            await asyncio.to_thread(json.dumps, payload)
            if action == "restore_public_data"
            else json.dumps(payload)
        )
        await self.connection.execute(
            """INSERT INTO admin_requests (telegram_account_id,token,owner_id,kind,action,payload)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb)""",
            self.bot_id,
            token,
            owner,
            kind,
            action,
            encoded,
        )
        return token

    async def attach_prompt(self, owner, token, message_id):
        await self.connection.execute(
            "UPDATE admin_requests SET message_id=$4 WHERE telegram_account_id=$1 AND owner_id=$2 AND token=$3 AND consumed_at IS NULL",
            self.bot_id,
            owner,
            token,
            message_id,
        )

    async def prompt(self, owner, message_id):
        row = await self.connection.fetchrow(
            """SELECT token,action FROM admin_requests WHERE telegram_account_id=$1
            AND owner_id=$2 AND message_id=$3 AND kind='prompt' AND consumed_at IS NULL
            AND expires_at>clock_timestamp()""",
            self.bot_id,
            owner,
            message_id,
        )
        return dict(row) if row else None

    async def consume(self, owner, token):
        row = await self.connection.fetchrow(
            """UPDATE admin_requests SET consumed_at=clock_timestamp()
            WHERE telegram_account_id=$1 AND owner_id=$2 AND token=$3 AND consumed_at IS NULL
            AND expires_at>clock_timestamp() RETURNING action,payload,created_at,kind""",
            self.bot_id,
            owner,
            token,
        )
        if row:
            data = dict(row)
            data["payload"] = (
                await asyncio.to_thread(json.loads, data["payload"])
                if data["action"] == "restore_public_data"
                else json.loads(data["payload"])
            )
            return data

    async def enqueue_job(self, owner, kind, payload, dedupe):
        return await self.connection.fetchval(
            """INSERT INTO admin_jobs (telegram_account_id,requested_by,kind,payload,dedupe_key)
            VALUES ($1,$2,$3,$4::jsonb,$5) ON CONFLICT (telegram_account_id,dedupe_key)
            DO UPDATE SET dedupe_key=EXCLUDED.dedupe_key RETURNING id""",
            self.bot_id,
            owner,
            kind,
            json.dumps(payload),
            dedupe,
        )

    async def jobs(self, limit=20):
        rows = await self.connection.fetch(
            f"SELECT {JOB_FIELDS} FROM admin_jobs WHERE telegram_account_id=$1 ORDER BY id DESC LIMIT $2",
            self.bot_id,
            limit,
        )
        return tuple(AdminJob(**dict(row)) for row in rows)

    async def cancel_job(self, identity):
        result = await self.connection.fetchval(
            """UPDATE admin_jobs SET status='cancelled',locked_by=NULL,locked_until=NULL,
            completed_at=now() WHERE telegram_account_id=$1 AND id=$2 AND status IN ('queued','running') RETURNING id""",
            self.bot_id,
            identity,
        )
        if result:
            count = await self.connection.fetchval(
                """WITH cancelled AS (UPDATE outbox_events SET status='cancelled',completed_at=now()
                WHERE telegram_account_id=$1 AND job_id=$2 AND status='pending' RETURNING id)
                SELECT count(*) FROM cancelled""",
                self.bot_id,
                identity,
            )
            await self.skipped(identity, count)
        return result

    async def skipped(self, identity, count):
        await self.connection.execute(
            "UPDATE admin_jobs SET skipped_count=skipped_count+$3 WHERE telegram_account_id=$1 AND id=$2",
            self.bot_id,
            identity,
            count,
        )

    async def mark_blocked(self, user_id, deleted=False):
        await self.connection.execute(
            "UPDATE bot_users SET is_blocked=TRUE,account_kind=CASE WHEN $3 THEN 'deleted' ELSE account_kind END,updated_at=now() WHERE telegram_account_id=$1 AND user_id=$2",
            self.bot_id,
            user_id,
            deleted,
        )

    async def import_users(self, rows):
        rows = list(rows)
        await self.connection.executemany(
            """INSERT INTO bot_users (telegram_account_id,user_id,username,first_name,last_name)
            VALUES ($1,$2,$3,$4,$5) ON CONFLICT (telegram_account_id,user_id) DO UPDATE SET
            username=COALESCE(bot_users.username,EXCLUDED.username),
            first_name=COALESCE(bot_users.first_name,EXCLUDED.first_name),
            last_name=COALESCE(bot_users.last_name,EXCLUDED.last_name),updated_at=now()""",
            [(self.bot_id, *row) for row in rows],
        )
        from repositories.postgres.user_tombstone import remove_tombstoned

        await remove_tombstoned(self.connection, self.bot_id, [row[0] for row in rows])
