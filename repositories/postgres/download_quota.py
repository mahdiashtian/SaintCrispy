"""Fresh SQL settings and atomic rolling admissions across chats and processes."""

import json
import math
from datetime import timedelta

from schemas.media import DownloadError

from .workflow import WorkflowRepository


class DownloadQuotaRepository:
    def __init__(self, pool, bot_id, *, group_default=5):
        self.pool, self.bot_id, self.group_default = pool, bot_id, group_default

    async def limits(self):
        rows = await self.pool.fetch(
            "SELECT key,value FROM app_settings WHERE telegram_account_id=$1 "
            "AND key IN ('user_links_per_minute','group_links_per_minute')",
            self.bot_id,
        )
        values = {row["key"]: json.loads(row["value"]) for row in rows}
        return {
            "user_limit": values.get("user_links_per_minute", 1),
            "group_limit": values.get("group_links_per_minute", self.group_default),
        }

    async def claim(self, user_id, scope, *, chat_id=None, request_id=None):
        if (
            type(user_id) is not int
            or not 0 < user_id < 2**63
            or scope not in {"inspect", "download"}
        ):
            raise DownloadError("درخواست معتبر نیست.", code="invalid_request_context")
        if scope == "inspect" and (
            type(chat_id) is not int or not -(2**63) < chat_id < 2**63 or chat_id == 0
        ):
            raise DownloadError("گفت‌وگو معتبر نیست.", code="invalid_request_context")
        async with self.pool.acquire() as connection, connection.transaction():
            group = None
            if scope == "inspect" and chat_id < 0:
                group = await connection.fetchrow(
                    """SELECT is_active,can_send,migrated_to,links_per_minute FROM bot_groups
                    WHERE telegram_account_id=$1 AND chat_id=$2 FOR UPDATE""",
                    self.bot_id,
                    chat_id,
                )
                if not group or group["is_active"] is not True or group["migrated_to"]:
                    raise DownloadError("ربات در این گروه فعال نیست.", code="group_unavailable")
                if group["can_send"] is False:
                    raise DownloadError(
                        "ربات در این گروه دسترسی ارسال ندارد.", code="group_send_forbidden"
                    )
            # Every operation takes group then user locks; two chats cannot grant
            # the same user's last slot concurrently, even in separate processes.
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1::text || ':' || $2::text, 617))",
                str(self.bot_id),
                str(user_id),
            )
            settings = await connection.fetch(
                """SELECT key,value FROM app_settings WHERE telegram_account_id=$1
                AND key IN ('user_links_per_minute','group_links_per_minute')""",
                self.bot_id,
            )
            values = {row["key"]: json.loads(row["value"]) for row in settings}
            limit = values.get("user_links_per_minute", 1)
            group_limit = (group["links_per_minute"] if group else None) or values.get(
                "group_links_per_minute", self.group_default
            )
            if any(type(v) is not int or not 1 <= v <= 1000 for v in (limit, group_limit)):
                raise DownloadError(
                    "تنظیمات محدودیت معتبر نیست.", code="quota_configuration_invalid"
                )
            now = await connection.fetchval("SELECT clock_timestamp()")
            rows = await connection.fetch(
                """SELECT scope,ARRAY(SELECT stamp FROM unnest(accepted_at) stamp
                WHERE stamp > $3::timestamptz - interval '60 seconds' ORDER BY stamp) AS stamps
                FROM download_request_windows WHERE telegram_account_id=$1 AND user_id=$2""",
                self.bot_id,
                user_id,
                now,
            )
            windows = {row["scope"]: row["stamps"] for row in rows}
            waits = [
                math.ceil((stamps[-limit] + timedelta(seconds=60) - now).total_seconds())
                for key, stamps in windows.items()
                if key in ({"inspect", "download"} if scope == "inspect" else {"download"})
                and len(stamps) >= limit
            ]
            if waits:
                return "user_rate_limited", max(1, *waits), limit
            accepted = []
            if group:
                accepted = (
                    await connection.fetchval(
                        """SELECT ARRAY(SELECT stamp FROM unnest(accepted_at) stamp
                    WHERE stamp > $3::timestamptz - interval '60 seconds' ORDER BY stamp)
                    FROM group_request_limits WHERE telegram_account_id=$1 AND chat_id=$2""",
                        self.bot_id,
                        chat_id,
                        now,
                    )
                    or []
                )
                if len(accepted) >= group_limit:
                    wait = math.ceil(
                        (accepted[-group_limit] + timedelta(seconds=60) - now).total_seconds()
                    )
                    return "group_rate_limited", max(1, wait), group_limit
            await connection.execute(
                """INSERT INTO download_request_windows (telegram_account_id,user_id,scope,accepted_at)
                VALUES ($1,$2,$3,$4) ON CONFLICT (telegram_account_id,user_id,scope)
                DO UPDATE SET accepted_at=EXCLUDED.accepted_at""",
                self.bot_id,
                user_id,
                scope,
                [*windows.get(scope, []), now],
            )
            if scope == "inspect":
                await WorkflowRepository(connection, self.bot_id).record_admitted_link(
                    user_id, chat_id, request_id
                )
                if group:
                    await connection.execute(
                        """INSERT INTO group_request_limits (telegram_account_id,chat_id,accepted_at)
                        VALUES ($1,$2,$3) ON CONFLICT (telegram_account_id,chat_id)
                        DO UPDATE SET accepted_at=EXCLUDED.accepted_at""",
                        self.bot_id,
                        chat_id,
                        [*accepted, now],
                    )
            return "accepted", 0, limit
