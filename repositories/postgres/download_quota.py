"""Fresh SQL settings and atomic rolling admissions across chats and processes."""

import json
import math
from datetime import timedelta

from schemas.download_limits import DownloadLimits
from schemas.media import DownloadError

from .workflow import WorkflowRepository


def window_rejection(rows, now, *, prefix, quota_enabled, limit, gap_enabled, gap_seconds):
    waits = []
    for row in rows:
        if quota_enabled and len(row["stamps"]) >= limit:
            waits.append(
                (
                    (row["stamps"][-limit] + timedelta(seconds=60) - now).total_seconds(),
                    f"{prefix}_rate_limited",
                    limit,
                )
            )
        if gap_enabled and row["latest"] is not None:
            waits.append(
                (
                    (row["latest"] + timedelta(seconds=gap_seconds) - now).total_seconds(),
                    f"{prefix}_gap_limited",
                    gap_seconds,
                )
            )
    if waits:
        wait, reason, value = max(waits, key=lambda item: item[0])
        if wait > 0:
            return reason, math.ceil(wait), value


class DownloadQuotaRepository:
    def __init__(self, pool, bot_id, *, group_default=5, main_admin_id=None):
        self.pool, self.bot_id, self.group_default = pool, bot_id, group_default
        self.main_admin_id = main_admin_id

    async def _configuration(self, connection):
        rows = await connection.fetch(
            "SELECT key,value FROM app_settings WHERE telegram_account_id=$1 AND key=ANY($2::text[])",
            self.bot_id,
            list(DownloadLimits().settings()),
        )
        try:
            return DownloadLimits.from_settings(
                {row["key"]: json.loads(row["value"]) for row in rows},
                group_default=self.group_default,
            )
        except (ValueError, TypeError) as error:
            raise DownloadError(
                "تنظیمات محدودیت معتبر نیست.", code="quota_configuration_invalid"
            ) from error

    async def limits(self):
        return (await self._configuration(self.pool)).public()

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
            # Installation roles are fresh SQL evidence, never group-admin status or cache.
            exempt = user_id == self.main_admin_id or bool(
                await connection.fetchval(
                    "SELECT is_admin AND NOT is_banned FROM bot_users "
                    "WHERE telegram_account_id=$1 AND user_id=$2",
                    self.bot_id,
                    user_id,
                )
            )
            if exempt:
                if scope == "inspect":
                    await WorkflowRepository(connection, self.bot_id).record_admitted_link(
                        user_id, chat_id, request_id
                    )
                return "accepted", 0, 0
            config = await self._configuration(connection)
            limit = config.user_links_per_minute
            group_limit = (
                group["links_per_minute"] if group else None
            ) or config.group_links_per_minute
            now = await connection.fetchval("SELECT clock_timestamp()")
            rows = await connection.fetch(
                """SELECT scope,ARRAY(SELECT stamp FROM unnest(accepted_at) stamp
                WHERE stamp > $3::timestamptz - interval '60 seconds' ORDER BY stamp) AS stamps,
                (SELECT max(stamp) FROM unnest(accepted_at) stamp) AS latest
                FROM download_request_windows WHERE telegram_account_id=$1 AND user_id=$2""",
                self.bot_id,
                user_id,
                now,
            )
            windows = {row["scope"]: row["stamps"] for row in rows}
            scopes = {"inspect", "download"} if scope == "inspect" else {"download"}
            rejected = window_rejection(
                [row for row in rows if row["scope"] in scopes],
                now,
                prefix="user",
                quota_enabled=config.user_quota_enabled,
                limit=limit,
                gap_enabled=config.user_gap_enabled,
                gap_seconds=config.user_gap_seconds,
            )
            if rejected:
                return rejected
            accepted = []
            if group:
                group_window = await connection.fetchrow(
                    """SELECT ARRAY(SELECT stamp FROM unnest(accepted_at) stamp
                    WHERE stamp > $3::timestamptz - interval '60 seconds' ORDER BY stamp) AS stamps,
                    (SELECT max(stamp) FROM unnest(accepted_at) stamp) AS latest
                    FROM group_request_limits WHERE telegram_account_id=$1 AND chat_id=$2""",
                    self.bot_id,
                    chat_id,
                    now,
                )
                accepted = group_window["stamps"] if group_window else []
                rejected = window_rejection(
                    [group_window] if group_window else [],
                    now,
                    prefix="group",
                    quota_enabled=config.group_quota_enabled,
                    limit=group_limit,
                    gap_enabled=config.group_gap_enabled,
                    gap_seconds=config.group_gap_seconds,
                )
                if rejected:
                    return rejected
            await connection.execute(
                """INSERT INTO download_request_windows (telegram_account_id,user_id,scope,accepted_at)
                VALUES ($1,$2,$3,$4) ON CONFLICT (telegram_account_id,user_id,scope)
                DO UPDATE SET accepted_at=EXCLUDED.accepted_at""",
                self.bot_id,
                user_id,
                scope,
                [*windows.get(scope, [])[-999:], now],
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
                        [*accepted[-999:], now],
                    )
            return "accepted", 0, limit
