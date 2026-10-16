"""Lease/CAS operations; every effect keeps its original Telegram random ID."""

import json
import secrets

from schemas.admin import PendingEffect


class OutboxRepository:
    def __init__(self, connection, bot_id):
        self.connection, self.bot_id = connection, bot_id

    async def enqueue(self, key, kind, target, payload, job_id=None):
        return await self.connection.fetchval(
            """INSERT INTO outbox_events (telegram_account_id,dedupe_key,kind,target_id,payload,random_id,job_id)
            VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7) ON CONFLICT DO NOTHING RETURNING id""",
            self.bot_id,
            key,
            kind,
            target,
            json.dumps(payload),
            secrets.randbits(63) or 1,
            job_id,
        )

    async def claim(self, owner, limit=4):
        rows = await self.connection.fetch(
            """WITH due AS (SELECT id FROM outbox_events WHERE telegram_account_id=$1
            AND status IN ('pending','leased') AND available_at<=clock_timestamp()
            AND (locked_until IS NULL OR locked_until<clock_timestamp())
            ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT $3)
            UPDATE outbox_events o SET status='leased',locked_by=$2,
            locked_until=clock_timestamp()+interval '60 seconds',attempt_count=attempt_count+1
            FROM due WHERE o.id=due.id RETURNING o.id,kind,target_id,payload,random_id,job_id,attempt_count""",
            self.bot_id,
            owner,
            limit,
        )
        return tuple(
            PendingEffect(**{**dict(row), "payload": json.loads(row["payload"])}) for row in rows
        )

    async def valid(self, identity, owner):
        return bool(
            await self.connection.fetchval(
                """SELECT 1 FROM outbox_events o LEFT JOIN admin_jobs j ON j.id=o.job_id
            WHERE o.telegram_account_id=$1 AND o.id=$2 AND o.locked_by=$3 AND o.status='leased'
            AND o.locked_until>clock_timestamp() AND (o.job_id IS NULL OR j.status<>'cancelled')""",
                self.bot_id,
                identity,
                owner,
            )
        )

    async def finish(self, identity, owner, status, error=None, delay=0):
        row = await self.connection.fetchrow(
            """UPDATE outbox_events SET status=$4,last_error=$5,
            available_at=clock_timestamp()+$6::float*interval '1 second',
            locked_by=NULL,locked_until=NULL,
            completed_at=CASE WHEN $4 IN ('sent','dead','cancelled') THEN now() ELSE NULL END
            WHERE telegram_account_id=$1 AND id=$2 AND locked_by=$3 AND status='leased'
            RETURNING job_id""",
            self.bot_id,
            identity,
            owner,
            status,
            error,
            float(delay),
        )
        if row and row["job_id"] and status in {"sent", "dead", "cancelled"}:
            await self.connection.execute(
                """UPDATE admin_jobs SET success_count=success_count+CASE WHEN $3='sent' THEN 1 ELSE 0 END,
                failure_count=failure_count+CASE WHEN $3='dead' THEN 1 ELSE 0 END,
                skipped_count=skipped_count+CASE WHEN $3='cancelled' THEN 1 ELSE 0 END
                WHERE telegram_account_id=$1 AND id=$2""",
                self.bot_id,
                row["job_id"],
                status,
            )
        return row is not None

    async def claim_job(self, owner):
        row = await self.connection.fetchrow(
            """WITH due AS (SELECT id FROM admin_jobs WHERE telegram_account_id=$1
            AND status IN ('queued','running') AND available_at<=clock_timestamp()
            AND payload->>'enumerated' IS DISTINCT FROM 'true'
            AND (locked_until IS NULL OR locked_until<clock_timestamp())
            ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE admin_jobs j SET status='running',locked_by=$2,
            locked_until=clock_timestamp()+interval '60 seconds',attempt_count=attempt_count+1
            FROM due WHERE j.id=due.id RETURNING j.*""",
            self.bot_id,
            owner,
        )
        if row:
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    async def targets(self, job, limit=50):
        audience = job["payload"].get("audience", "groups")
        cursor = job["cursor"]
        if job["kind"] == "purge_users":
            rows = await self.connection.fetch(
                "SELECT user_id id FROM bot_users WHERE telegram_account_id=$1 AND user_id>COALESCE($2::bigint,0) AND first_seen_at<=$3 ORDER BY user_id LIMIT $4",
                self.bot_id,
                cursor,
                job["created_at"],
                limit,
            )
        elif audience == "users" or job["kind"] == "refresh_users":
            rows = await self.connection.fetch(
                """SELECT user_id id FROM bot_users WHERE telegram_account_id=$1
                AND user_id>COALESCE($2::bigint,0) AND (is_started OR $5) AND NOT is_blocked AND NOT is_banned
                AND first_seen_at<=$3 ORDER BY user_id LIMIT $4""",
                self.bot_id,
                cursor,
                job["created_at"],
                limit,
                job["kind"] == "refresh_users",
            )
        else:
            rows = await self.connection.fetch(
                """SELECT chat_id id FROM bot_groups WHERE telegram_account_id=$1
                AND ($2::bigint IS NULL OR chat_id>$2) AND migrated_to IS NULL
                AND (is_active IS TRUE OR $5 OR $8) AND (can_send IS DISTINCT FROM FALSE OR $5 OR $8)
                AND created_at<=$3 AND NOT COALESCE(added_by_user_id=ANY($6::bigint[]),FALSE)
                AND (NOT $7 OR is_admin IS DISTINCT FROM TRUE)
                AND (NOT $8 OR is_active IS FALSE) AND (NOT $9 OR is_admin IS TRUE)
                AND ($10='both' OR ($10='admin' AND is_admin IS TRUE) OR ($10='non_admin' AND is_admin IS FALSE))
                AND NOT EXISTS(SELECT 1 FROM group_memberships m WHERE m.telegram_account_id=$1 AND m.chat_id=bot_groups.chat_id AND m.is_active AND m.user_id=ANY($11::bigint[])) ORDER BY chat_id LIMIT $4""",
                self.bot_id,
                cursor,
                job["created_at"],
                limit,
                job["kind"] == "health",
                job["payload"].get("exclude_adders", []),
                job["kind"] in {"nudge_admin", "nudge_owners"},
                job["kind"] == "nudge_owners",
                job["kind"] == "crawl",
                job["payload"].get("group_audience", "both"),
                job["payload"].get("exclude_members", []),
            )
        return [row["id"] for row in rows]

    async def started_recipients(self, identities):
        rows = await self.connection.fetch(
            "SELECT user_id FROM bot_users WHERE telegram_account_id=$1 AND user_id=ANY($2::bigint[]) AND is_started AND NOT is_blocked AND NOT is_banned ORDER BY user_id",
            self.bot_id,
            list(identities),
        )
        return tuple(row["user_id"] for row in rows)

    async def checkpoint(self, job, owner, cursor, added, done=False):
        return await self.connection.fetchval(
            """UPDATE admin_jobs SET cursor=$4,total_count=total_count+$5,
            payload=jsonb_set(payload,'{enumerated}',to_jsonb($6::boolean)),
            available_at=clock_timestamp()+interval '1 second',locked_by=NULL,locked_until=NULL,
            attempt_count=0,last_error=NULL
            WHERE telegram_account_id=$1 AND id=$2 AND locked_by=$3 AND status='running'
            AND locked_until>clock_timestamp() RETURNING id""",
            self.bot_id,
            job["id"],
            owner,
            cursor,
            added,
            done,
        )

    async def owned_job(self, identity, owner):
        return bool(
            await self.connection.fetchval(
                """SELECT id FROM admin_jobs WHERE telegram_account_id=$1 AND id=$2
            AND locked_by=$3 AND locked_until>clock_timestamp() AND status='running'
            FOR UPDATE""",
                self.bot_id,
                identity,
                owner,
            )
        )

    async def fail_job(self, identity, owner, code, delay, permanent=False):
        await self.connection.execute(
            """UPDATE admin_jobs SET status=CASE WHEN $6 THEN 'failed' ELSE 'queued' END,
            last_error=$4,available_at=clock_timestamp()+$5::float*interval '1 second',
            locked_by=NULL,locked_until=NULL WHERE telegram_account_id=$1 AND id=$2
            AND locked_by=$3 AND status='running'""",
            self.bot_id,
            identity,
            owner,
            code,
            float(delay),
            permanent,
        )

    async def job_owner(self, identity):
        row = await self.connection.fetchrow(
            "SELECT requested_by,kind,status FROM admin_jobs WHERE telegram_account_id=$1 AND id=$2",
            self.bot_id,
            identity,
        )
        return dict(row) if row else None

    async def settle_jobs(self):
        await self.connection.execute(
            """UPDATE admin_jobs j SET status='done',completed_at=now() WHERE telegram_account_id=$1
            AND status='running' AND payload->>'enumerated'='true'
            AND NOT EXISTS (SELECT 1 FROM outbox_events o WHERE o.job_id=j.id AND o.status IN ('pending','leased'))""",
            self.bot_id,
        )
