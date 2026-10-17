"""Group snapshot, immutable audit and quota writes on an owned transaction."""

import json
import math
from datetime import timedelta

from repositories.postgres.workflow import WorkflowRepository
from schemas.group import GroupChange, GroupSnapshot, SavedGroupPeer

GROUP_FIELDS = "chat_id,chat_kind,title,username,is_active,is_admin,can_send,added_by_user_id,added_at,readded_at,removed_at,admin_since,last_seen_at,last_event_at,migrated_to,owner_id,admin_ids,member_count,link,last_activity_at,links_per_minute,revision"


def snapshot(row):
    data = dict(row)
    data["admin_ids"] = tuple(json.loads(data["admin_ids"]))
    return GroupSnapshot(**data)


class GroupRepository:
    def __init__(self, connection, bot_id):
        self.connection, self.bot_id = connection, bot_id

    async def get(self, chat_id: int) -> GroupSnapshot | None:
        row = await self.connection.fetchrow(
            """SELECT chat_id,chat_kind,title,username,is_active,is_admin,can_send,
            added_by_user_id,added_at,readded_at,removed_at,admin_since,last_seen_at,
            last_event_at,migrated_to,owner_id,admin_ids,member_count,link,last_activity_at,
            links_per_minute,revision FROM bot_groups
            WHERE telegram_account_id=$1 AND chat_id=$2""",
            self.bot_id,
            chat_id,
        )
        if row is None:
            return None
        return snapshot(row)

    async def page(self, cursor=None, limit=20):
        rows = await self.connection.fetch(
            f"SELECT {GROUP_FIELDS} FROM bot_groups WHERE telegram_account_id=$1 AND ($2::bigint IS NULL OR chat_id>$2) ORDER BY chat_id LIMIT $3",
            self.bot_id,
            cursor,
            limit,
        )
        return tuple(snapshot(row) for row in rows)

    async def peer(self, chat_id):
        row = await self.connection.fetchrow(
            "SELECT chat_id,raw_id,chat_kind,access_hash FROM telegram_group_peers WHERE telegram_account_id=$1 AND chat_id=$2",
            self.bot_id,
            chat_id,
        )
        return SavedGroupPeer(**dict(row)) if row else None

    async def member_count(self, chat_id):
        return await self.connection.fetchval(
            "SELECT count(*) FROM group_memberships WHERE telegram_account_id=$1 AND chat_id=$2",
            self.bot_id,
            chat_id,
        )

    async def remember_peer(self, chat_id, raw_id, chat_kind, access_hash):
        await self.connection.execute(
            """INSERT INTO telegram_group_peers (telegram_account_id,chat_id,raw_id,chat_kind,access_hash)
            SELECT $1,$2,$3,$4,$5 WHERE EXISTS (SELECT 1 FROM bot_groups WHERE telegram_account_id=$1 AND chat_id=$2)
            ON CONFLICT (telegram_account_id,chat_id) DO UPDATE SET access_hash=EXCLUDED.access_hash,
            updated_at=now() WHERE telegram_group_peers.access_hash IS DISTINCT FROM EXCLUDED.access_hash""",
            self.bot_id,
            chat_id,
            raw_id,
            chat_kind,
            access_hash,
        )

    async def metadata(self, chat_id, owner_id, admin_ids, member_count, link):
        await self.connection.execute(
            """UPDATE bot_groups SET owner_id=COALESCE($3,owner_id),admin_ids=$4::jsonb,
            member_count=COALESCE($5,member_count),link=COALESCE($6,link),updated_at=now(),revision=revision+1
            WHERE telegram_account_id=$1 AND chat_id=$2""",
            self.bot_id,
            chat_id,
            owner_id,
            json.dumps(admin_ids),
            member_count,
            link,
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
        if change.kind in {"standing_refreshed", "permissions_changed"} and newer:
            if active is False and row["is_active"] is True:
                kind = "removed"
            elif active is True and row["is_active"] is False:
                kind = "readded"
            elif admin is True and row["is_admin"] is not True:
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
            removed = (
                change.occurred_at
                if active is False and row["is_active"] is not False
                else row["removed_at"]
            )
            admin_since = (row["admin_since"] or change.occurred_at) if admin else None
            await self.connection.execute(
                """UPDATE bot_groups SET title=COALESCE($3,title),username=COALESCE($4,username),
                is_active=$5,is_admin=$6,can_send=$7,
                added_by_user_id=CASE WHEN added_by_user_id IS NULL
                    AND $8::bigint IS NOT NULL AND $9 IN ('added','readded')
                    THEN $8 ELSE added_by_user_id END,
                added_at=$10,readded_at=$11,removed_at=$12,admin_since=$13,
                last_seen_at=GREATEST(last_seen_at,$14),
                last_event_at=CASE WHEN $18 THEN last_event_at ELSE $14 END,
                last_event_priority=CASE WHEN $18 THEN last_event_priority ELSE $15 END,
                event_cursors=$16::jsonb,
                migrated_to=COALESCE($17,migrated_to),updated_at=now(),revision=revision+1
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
            "previous_admin": row["is_admin"],
            "previous_can_send": row["can_send"],
            "kind": kind,
            "applied": newer,
            "is_active": active if newer else row["is_active"],
            "is_admin": admin if newer else row["is_admin"],
            "can_send": can_send if newer else row["can_send"],
            "transition": newer and (row["is_active"] != active or row["is_admin"] != admin),
        }

    async def claim_link(self, user_id, chat_id, request_id, interval, limit):
        # The group row serializes admissions and removal/migration in this transaction.
        group = await self.connection.fetchrow(
            """SELECT is_active,can_send,migrated_to,links_per_minute FROM bot_groups
            WHERE telegram_account_id=$1 AND chat_id=$2 FOR UPDATE""",
            self.bot_id,
            chat_id,
        )
        if group is None or group["is_active"] is not True or group["migrated_to"]:
            return "group_unavailable", 0
        if group["can_send"] is False:
            return "group_send_forbidden", 0
        limit = group["links_per_minute"] or limit
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
        before = await self.get(old_id)
        target_before = await self.get(new_id)
        result = await self.record(
            GroupChange(
                old_id, "migrated", key, occurred_at, "group", active=False, migrated_to=new_id
            )
        )
        await self.record(
            GroupChange(
                new_id,
                "migrated",
                key,
                occurred_at,
                "supergroup",
                active=True
                if target_before.is_active is None and before.is_active is not False
                else None,
                admin=before.is_admin
                if target_before.is_admin is None and target_before.is_active is not False
                else None,
                can_send=before.can_send
                if target_before.can_send is None and target_before.is_active is not False
                else None,
            )
        )
        if result:
            if (
                target_before.is_admin is None
                and before.is_admin is True
                and before.admin_since is not None
            ):
                await self.connection.execute(
                    "UPDATE bot_groups SET admin_since=$3 WHERE telegram_account_id=$1 AND chat_id=$2 AND is_admin IS TRUE",
                    self.bot_id,
                    new_id,
                    before.admin_since,
                )
            # Migration is a permanent identity mapping, even if a later removal
            # already supplied the more recent membership snapshot.
            await self.connection.execute(
                """UPDATE bot_groups SET migrated_to=$3,is_active=FALSE,is_admin=FALSE,
                can_send=FALSE,admin_since=NULL,updated_at=now()
                WHERE telegram_account_id=$1 AND chat_id=$2""",
                self.bot_id,
                old_id,
                new_id,
            )
            await self.connection.execute(
                """UPDATE bot_groups target SET title=COALESCE(target.title,old.title),
                added_by_user_id=COALESCE(target.added_by_user_id,old.added_by_user_id),
                owner_id=COALESCE(target.owner_id,old.owner_id),
                admin_ids=CASE WHEN target.admin_ids='[]'::jsonb THEN old.admin_ids ELSE target.admin_ids END,
                member_count=COALESCE(target.member_count,old.member_count),
                links_per_minute=COALESCE(target.links_per_minute,old.links_per_minute),
                added_at=LEAST(target.added_at,old.added_at),
                last_activity_at=GREATEST(target.last_activity_at,old.last_activity_at)
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
