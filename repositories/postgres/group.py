"""Group snapshot, immutable audit and quota writes on an owned transaction."""

import json
import math
from datetime import timedelta

from repositories.postgres.workflow import WorkflowRepository
from schemas.group import GroupChange


class GroupRepository:
    def __init__(self, connection, bot_id):
        self.connection, self.bot_id = connection, bot_id

    async def get(self, chat_id):
        return await self.connection.fetchrow(
            "SELECT * FROM bot_groups WHERE telegram_account_id=$1 AND chat_id=$2",
            self.bot_id,
            chat_id,
        )

    async def _ensure(self, chat_id, chat_kind):
        await self.connection.execute(
            """INSERT INTO bot_groups (telegram_account_id,chat_id,chat_kind)
            VALUES ($1,$2,$3) ON CONFLICT DO NOTHING""",
            self.bot_id,
            chat_id,
            chat_kind,
        )

    async def record(self, change: GroupChange):
        await self._ensure(change.chat_id, change.chat_kind)
        row = await self.connection.fetchrow(
            """SELECT * FROM bot_groups WHERE telegram_account_id=$1 AND chat_id=$2
            FOR UPDATE""",
            self.bot_id,
            change.chat_id,
        )
        cursors = json.loads(row["event_cursors"])
        newer = change.occurred_at > row["last_event_at"] or (
            change.occurred_at == row["last_event_at"]
            and change.priority >= row["last_event_priority"]
        )
        if change.sequence:
            previous = cursors.get(change.stream, 0)
            newer = change.sequence > previous and (
                newer
                or (change.stream == "basic" and previous > 0 and row["last_event_priority"] == 2)
            )
        # Ordinary message discovery never reverses known lifecycle evidence.
        observed = change.kind == "observed"
        active = row["is_active"] if observed and row["is_active"] is not None else change.active
        admin = row["is_admin"] if observed and row["is_admin"] is not None else change.admin
        active = row["is_active"] if active is None else active
        admin = row["is_admin"] if admin is None else admin
        if active and row["is_active"] is False and change.admin is None:
            admin = False
        if active is False:
            admin = False
        can_send = change.can_send if change.can_send is not None else row["can_send"]
        if observed and row["can_send"] is not None:
            can_send = row["can_send"]
        if active and row["is_active"] is False:
            can_send = change.can_send
        if active is False:
            can_send = False
        if row["migrated_to"] is not None:
            active, admin, can_send = False, False, False
        kind = "discovered" if observed and row["is_active"] is None else change.kind
        if change.kind == "added" and row["is_active"] is False:
            kind = "readded"
        if change.kind == "standing_refreshed" and newer:
            if active is False and row["is_active"] is True:
                kind = "removed"
            elif active is True and row["is_active"] is False:
                kind = "readded"
            elif admin is True and row["is_admin"] is False:
                kind = "promoted"
            elif admin is False and row["is_admin"] is True:
                kind = "demoted"
        event = await self.connection.fetchrow(
            """INSERT INTO group_events (telegram_account_id,chat_id,event_key,kind,
            actor_id,occurred_at,applied,previous_active,previous_admin,is_active,is_admin,can_send)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT DO NOTHING RETURNING id""",
            self.bot_id,
            change.chat_id,
            change.event_key,
            kind,
            change.actor_id,
            change.occurred_at,
            newer,
            row["is_active"],
            row["is_admin"],
            active if newer else row["is_active"],
            admin if newer else row["is_admin"],
            can_send if newer else row["can_send"],
        )
        if event is None:
            return None
        if newer:
            if change.sequence:
                cursors[change.stream] = change.sequence
            added = row["added_at"] or (change.occurred_at if active else None)
            readded = (
                change.occurred_at if active and row["is_active"] is False else row["readded_at"]
            )
            removed = change.occurred_at if active is False else row["removed_at"]
            admin_since = (row["admin_since"] or change.occurred_at) if admin else None
            await self.connection.execute(
                """UPDATE bot_groups SET title=COALESCE($3,title),username=COALESCE($4,username),
                is_active=$5,is_admin=$6,can_send=$7,
                added_by_user_id=CASE WHEN $8::bigint IS NOT NULL AND $9 IN ('added','readded')
                    THEN $8 ELSE added_by_user_id END,
                added_at=$10,readded_at=$11,removed_at=$12,admin_since=$13,
                last_seen_at=GREATEST(last_seen_at,$14),
                last_event_at=CASE WHEN $18 THEN last_event_at ELSE $14 END,
                last_event_priority=CASE WHEN $18 THEN last_event_priority ELSE $15 END,
                event_cursors=$16::jsonb,
                migrated_to=COALESCE($17,migrated_to),updated_at=now()
                WHERE telegram_account_id=$1 AND chat_id=$2""",
                self.bot_id,
                change.chat_id,
                change.title,
                change.username,
                active,
                admin,
                can_send,
                change.actor_id,
                kind,
                added,
                readded,
                removed,
                admin_since,
                change.occurred_at,
                change.priority,
                json.dumps(cursors),
                change.migrated_to,
                observed and row["is_active"] is not None,
            )
        return {
            "event_id": event["id"],
            "kind": kind,
            "applied": newer,
            "is_active": active if newer else row["is_active"],
            "is_admin": admin if newer else row["is_admin"],
            "can_send": can_send if newer else row["can_send"],
        }

    async def claim_link(self, user_id, chat_id, request_id, interval, limit):
        # The group row serializes admissions and removal/migration in this transaction.
        group = await self.connection.fetchrow(
            """SELECT is_active,can_send,migrated_to FROM bot_groups
            WHERE telegram_account_id=$1 AND chat_id=$2 FOR UPDATE""",
            self.bot_id,
            chat_id,
        )
        if group is None or group["is_active"] is not True or group["migrated_to"]:
            return "group_unavailable", 0
        if group["can_send"] is False:
            return "group_send_forbidden", 0
        await self.connection.execute(
            """INSERT INTO group_request_limits (telegram_account_id,chat_id)
            VALUES ($1,$2) ON CONFLICT DO NOTHING""",
            self.bot_id,
            chat_id,
        )
        now = await self.connection.fetchval("SELECT clock_timestamp()")
        accepted = await self.connection.fetchval(
            """SELECT ARRAY(SELECT stamp FROM unnest(accepted_at) stamp
            WHERE stamp > $3::timestamptz - interval '60 seconds' ORDER BY stamp)
            FROM group_request_limits WHERE telegram_account_id=$1 AND chat_id=$2""",
            self.bot_id,
            chat_id,
            now,
        )
        if len(accepted) >= limit:
            return "group_rate_limited", max(
                1, math.ceil((accepted[-limit] + timedelta(seconds=60) - now).total_seconds())
            )
        remaining = await WorkflowRepository(self.connection, self.bot_id).claim_link(
            user_id, chat_id, request_id, interval
        )
        if remaining:
            return "user_rate_limited", remaining
        await self.connection.execute(
            """UPDATE group_request_limits SET accepted_at=$3::timestamptz[]
            WHERE telegram_account_id=$1 AND chat_id=$2""",
            self.bot_id,
            chat_id,
            [*accepted, now],
        )
        return "accepted", 0

    async def migrate(self, old_id, new_id, occurred_at):
        # Consistent lock order also protects concurrent admissions and replayed migrations.
        for chat_id in sorted((old_id, new_id)):
            await self._ensure(chat_id, "group" if chat_id == old_id else "supergroup")
        await self.connection.fetch(
            """SELECT chat_id FROM bot_groups WHERE telegram_account_id=$1
            AND chat_id=ANY($2::bigint[]) ORDER BY chat_id FOR UPDATE""",
            self.bot_id,
            [old_id, new_id],
        )
        key = f"migration:{old_id}:{new_id}"
        result = await self.record(
            GroupChange(
                old_id, "migrated", key, occurred_at, "group", active=False, migrated_to=new_id
            )
        )
        await self.record(
            GroupChange(new_id, "migrated", key, occurred_at, "supergroup", active=True)
        )
        if result:
            # Migration is a permanent identity mapping, even if a later removal
            # already supplied the more recent membership snapshot.
            await self.connection.execute(
                """UPDATE bot_groups SET migrated_to=$3,is_active=FALSE,is_admin=FALSE,
                can_send=FALSE,updated_at=now() WHERE telegram_account_id=$1 AND chat_id=$2""",
                self.bot_id,
                old_id,
                new_id,
            )
            await self.connection.execute(
                """UPDATE bot_groups target SET title=COALESCE(target.title,old.title),
                added_by_user_id=COALESCE(target.added_by_user_id,old.added_by_user_id)
                FROM bot_groups old WHERE target.telegram_account_id=$1 AND target.chat_id=$3
                AND old.telegram_account_id=$1 AND old.chat_id=$2""",
                self.bot_id,
                old_id,
                new_id,
            )
            await self.connection.execute(
                """INSERT INTO group_request_limits (telegram_account_id,chat_id,accepted_at)
                SELECT $1,$3,accepted_at FROM group_request_limits
                WHERE telegram_account_id=$1 AND chat_id=$2
                ON CONFLICT (telegram_account_id,chat_id) DO UPDATE SET
                accepted_at=group_request_limits.accepted_at || EXCLUDED.accepted_at""",
                self.bot_id,
                old_id,
                new_id,
            )
        return result
