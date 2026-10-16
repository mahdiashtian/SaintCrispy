"""A membership event and its exact counter commit on one owned connection."""

from schemas.membership import MembershipEvent


class MembershipRepository:
    def __init__(self, connection, bot_id):
        self.connection, self.bot_id = connection, bot_id

    async def apply_messages(self, events):
        from repositories.postgres.activity_batch import apply_messages

        return await apply_messages(self.connection, self.bot_id, events)

    async def known_member(self, chat_id, user_id):
        return bool(
            await self.connection.fetchval(
                "SELECT 1 FROM bot_users u JOIN bot_groups g USING(telegram_account_id) WHERE u.telegram_account_id=$1 AND g.chat_id=$2 AND u.user_id=$3 AND u.account_kind='real'",
                self.bot_id,
                chat_id,
                user_id,
            )
        )

    async def departure(self, chat_id, user_id, occurred_at, event_key):
        await self.connection.execute(
            """INSERT INTO membership_departures(telegram_account_id,chat_id,user_id,revision,occurred_at,event_key)
        SELECT $1,$2,$3,nextval('membership_state_revision'),$4,$5 FROM bot_groups WHERE telegram_account_id=$1 AND chat_id=$2
        ON CONFLICT(telegram_account_id,chat_id,user_id) DO UPDATE SET revision=EXCLUDED.revision,
        occurred_at=EXCLUDED.occurred_at,event_key=EXCLUDED.event_key
        WHERE membership_departures.event_key<>EXCLUDED.event_key AND membership_departures.occurred_at<=EXCLUDED.occurred_at""",
            self.bot_id,
            chat_id,
            user_id,
            occurred_at,
            event_key,
        )

    async def known_active(self, chat_id, user_id):
        return bool(
            await self.connection.fetchval(
                """SELECT is_active FROM group_memberships WHERE telegram_account_id=$1
            AND chat_id=$2 AND user_id=$3""",
                self.bot_id,
                chat_id,
                user_id,
            )
        )

    async def profile(self, user_id, username, first_name, last_name):
        await self.connection.execute(
            """INSERT INTO bot_users (telegram_account_id,user_id,username,first_name,last_name,account_kind)
            VALUES ($1,$2,$3,$4,$5,'real') ON CONFLICT (telegram_account_id,user_id) DO UPDATE
            SET username=EXCLUDED.username,first_name=EXCLUDED.first_name,last_name=EXCLUDED.last_name,
            account_kind='real',is_blocked=FALSE,last_seen_at=now(),updated_at=now()""",
            self.bot_id,
            user_id,
            username,
            first_name,
            last_name,
        )
        from repositories.postgres.user_tombstone import remove_tombstoned

        await remove_tombstoned(self.connection, self.bot_id, [user_id])

    async def refresh_profile(
        self, user_id, username, first_name, last_name, *, deleted=False, bot=False
    ):
        await self.connection.execute(
            """UPDATE bot_users SET username=$3,first_name=$4,last_name=$5,
        account_kind=$6,updated_at=now() WHERE telegram_account_id=$1 AND user_id=$2""",
            self.bot_id,
            user_id,
            username,
            first_name,
            last_name,
            "deleted" if deleted else "bot" if bot else "real",
        )

    async def purge_invalid(self, user_id, *, deleted=False, bot=False):
        if not (deleted or bot):
            return False
        eligible = await self.connection.fetchval(
            "SELECT NOT is_admin FROM bot_users WHERE telegram_account_id=$1 AND user_id=$2 FOR UPDATE",
            self.bot_id,
            user_id,
        )
        if not eligible:
            return False
        await self.connection.execute(
            """INSERT INTO user_account_tombstones(telegram_account_id,user_id,account_kind)
        SELECT $1,$2,$3 FROM bot_users WHERE telegram_account_id=$1 AND user_id=$2 AND NOT is_admin
        ON CONFLICT(telegram_account_id,user_id) DO NOTHING""",
            self.bot_id,
            user_id,
            "deleted" if deleted else "bot",
        )
        return bool(
            await self.connection.fetchval(
                "DELETE FROM bot_users WHERE telegram_account_id=$1 AND user_id=$2 AND NOT is_admin RETURNING user_id",
                self.bot_id,
                user_id,
            )
        )

    async def apply(self, event: MembershipEvent):
        group = await self.connection.fetchrow(
            """SELECT is_active,is_admin,migrated_to FROM bot_groups WHERE telegram_account_id=$1
            AND chat_id=$2 FOR KEY SHARE""",
            self.bot_id,
            event.chat_id,
        )
        if group is None or group["migrated_to"] is not None:
            return False
        if event.kind == "left" and not await self.connection.fetchval(
            "SELECT 1 FROM bot_users WHERE telegram_account_id=$1 AND user_id=$2 AND account_kind='real'",
            self.bot_id,
            event.user_id,
        ):
            return False
        await self.connection.execute(
            """INSERT INTO bot_users (telegram_account_id,user_id,username,first_name,last_name,
            account_kind,last_seen_at) VALUES ($1,$2,$3,$4,$5,'real',$6)
            ON CONFLICT (telegram_account_id,user_id) DO UPDATE SET
            username=COALESCE(EXCLUDED.username,bot_users.username),
            first_name=COALESCE(EXCLUDED.first_name,bot_users.first_name),
            last_name=COALESCE(EXCLUDED.last_name,bot_users.last_name),account_kind='real',
            last_seen_at=GREATEST(bot_users.last_seen_at,EXCLUDED.last_seen_at)""",
            self.bot_id,
            event.user_id,
            event.username,
            event.first_name,
            event.last_name,
            event.occurred_at,
        )
        from repositories.postgres.user_tombstone import remove_tombstoned

        if await remove_tombstoned(self.connection, self.bot_id, [event.user_id]):
            return False
        await self.connection.execute(
            """INSERT INTO group_memberships (telegram_account_id,chat_id,user_id,is_active,
            state_changed_at,state_event_stream,state_revision)
            SELECT $1,$2,$3,FALSE,COALESCE(d.occurred_at,'epoch'),
            CASE WHEN d.user_id IS NULL THEN 'message' ELSE 'departure' END,COALESCE(d.revision,0)
            FROM (SELECT 1) seed LEFT JOIN membership_departures d
            ON d.telegram_account_id=$1 AND d.chat_id=$2 AND d.user_id=$3
            ON CONFLICT DO NOTHING""",
            self.bot_id,
            event.chat_id,
            event.user_id,
        )
        # Serialize all event streams for this member, before checking the dedupe ledger.
        row = await self.connection.fetchrow(
            """SELECT * FROM group_memberships WHERE telegram_account_id=$1 AND chat_id=$2
            AND user_id=$3 FOR UPDATE""",
            self.bot_id,
            event.chat_id,
            event.user_id,
        )
        inserted = await self.connection.fetchval(
            """INSERT INTO membership_events (telegram_account_id,chat_id,event_key,user_id,
            kind,occurred_at,observed_admin) VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING RETURNING 1""",
            self.bot_id,
            event.chat_id,
            event.event_key,
            event.user_id,
            event.kind,
            event.occurred_at,
            event.observed_admin,
        )
        if inserted is None:
            return False
        newer = event.occurred_at > row["state_changed_at"] or (
            event.occurred_at == row["state_changed_at"]
            and (event.sequence > row["state_event_order"] or event.kind == "left")
        )
        if (
            event.occurred_at == row["state_changed_at"]
            and event.stream != row["state_event_stream"]
        ):
            priority = {
                "roster": -1,
                "message": 0,
                "service": 1,
                "basic": 2,
                "participant": 2,
                "departure": 3,
            }
            newer = priority[event.stream] > priority[row["state_event_stream"]]
        active = event.kind != "left"
        if row["state_event_stream"] == "roster" and event.kind != "message":
            newer = event.occurred_at >= row["state_changed_at"].replace(microsecond=0)
        if (
            event.stream == row["state_event_stream"]
            and event.stream != "message"
            and event.sequence
        ):
            newer = event.sequence > row["state_event_order"]
        if (
            event.kind == "message"
            and event.occurred_at == row["state_changed_at"]
            and not row["is_active"]
        ):
            newer = False
        if event.kind == "message" and group["is_active"] is not True:
            active = row["is_active"]
        await self.connection.execute(
            """UPDATE group_memberships SET
            is_active=CASE WHEN $4 THEN $5 ELSE is_active END,
            state_changed_at=CASE WHEN $4 THEN $6 ELSE state_changed_at END,
            state_event_order=CASE WHEN $4 AND $9<>'message' THEN $7 ELSE state_event_order END,
            state_event_stream=CASE WHEN $4 AND $9<>'message' THEN $9 ELSE state_event_stream END,
            last_seen_at=CASE WHEN $8 THEN GREATEST(last_seen_at,$6) ELSE last_seen_at END,
            message_count=message_count+CASE WHEN $8 THEN 1 ELSE 0 END,
            state_revision=CASE WHEN $4 THEN nextval('membership_state_revision') ELSE state_revision END,
            msg_this_week=CASE WHEN NOT $8 THEN msg_this_week WHEN msg_week=$10 THEN msg_this_week+1 WHEN msg_week<$10 THEN 1 ELSE msg_this_week END,
            msg_last_week=CASE WHEN NOT $8 THEN msg_last_week WHEN msg_week=$10-1 THEN msg_this_week WHEN msg_week<$10-1 THEN 0 WHEN msg_week=$10+1 THEN msg_last_week+1 ELSE msg_last_week END,
            msg_week=CASE WHEN $8 THEN GREATEST(msg_week,$10) ELSE msg_week END,updated_at=now()
            WHERE telegram_account_id=$1 AND chat_id=$2 AND user_id=$3""",
            self.bot_id,
            event.chat_id,
            event.user_id,
            newer,
            active,
            event.occurred_at,
            event.sequence,
            event.kind == "message",
            event.stream,
            int(event.occurred_at.timestamp()) // 604800,
        )
        if event.kind == "message":
            await self.connection.execute(
                "UPDATE bot_users SET last_active_at=GREATEST(last_active_at,$3) WHERE telegram_account_id=$1 AND user_id=$2",
                self.bot_id,
                event.user_id,
                event.occurred_at,
            )
            await self.connection.execute(
                """UPDATE bot_groups SET last_activity_at=GREATEST(last_activity_at,$3)
                WHERE telegram_account_id=$1 AND chat_id=$2""",
                self.bot_id,
                event.chat_id,
                event.occurred_at,
            )
        return True
