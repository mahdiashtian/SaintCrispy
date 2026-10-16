"""Atomic crawl claims, member fences and page checkpoints on an owned UoW."""

import uuid
from datetime import timedelta

from schemas.roster_crawl import RosterCrawl

FIELDS = ",".join(RosterCrawl.__dataclass_fields__)


class RosterCrawlRepository:
    def __init__(self, connection, bot_id):
        self.connection, self.bot_id = connection, bot_id

    async def get(self, chat_id):
        row = await self.connection.fetchrow(
            f"SELECT {FIELDS} FROM group_roster_crawls WHERE telegram_account_id=$1 AND chat_id=$2",
            self.bot_id,
            chat_id,
        )
        return RosterCrawl(**dict(row)) if row else None

    async def enqueue(self, chat_id):
        woke = await self.connection.fetchval(
            """UPDATE group_roster_crawls c SET available_at=now(),stopped_because=NULL
        FROM bot_groups g WHERE c.telegram_account_id=$1 AND c.chat_id=$2 AND c.status='paused' AND c.stopped_because='not_admin'
        AND g.telegram_account_id=c.telegram_account_id AND g.chat_id=c.chat_id AND g.is_active IS TRUE AND g.is_admin IS TRUE AND g.migrated_to IS NULL RETURNING c.run_id""",
            self.bot_id,
            chat_id,
        )
        if woke:
            return True
        value = await self.connection.fetchval(
            """INSERT INTO group_roster_crawls
        (telegram_account_id,chat_id,run_id) SELECT $1,$2,$3 FROM bot_groups
        WHERE telegram_account_id=$1 AND chat_id=$2 AND is_active IS TRUE AND is_admin IS TRUE AND migrated_to IS NULL
        ON CONFLICT(telegram_account_id,chat_id) DO UPDATE SET run_id=EXCLUDED.run_id,
        status='queued',pass_number=1,filter_index=0,offset_count=0,pass_new_count=0,
        claimed_total=NULL,seen_unique=0,stored_new=0,skipped_accounts=0,requests_made=0,
        flood_waits=0,ambiguous_visits=0,converged=FALSE,stopped_because=NULL,last_error=NULL,
        locked_by=NULL,locked_until=NULL,available_at=now(),started_at=NULL,finished_at=NULL,updated_at=now()
        WHERE group_roster_crawls.status IN ('done','failed','cancelled') RETURNING run_id""",
            self.bot_id,
            chat_id,
            uuid.uuid4().hex,
        )
        if value:
            await self.connection.execute(
                "DELETE FROM roster_crawl_seen WHERE telegram_account_id=$1 AND chat_id=$2 AND run_id<>$3",
                self.bot_id,
                chat_id,
                value,
            )
        return value is not None

    async def seed_missing(self, limit=100):
        rows = await self.connection.fetch(
            """SELECT g.chat_id FROM bot_groups g
        LEFT JOIN group_roster_crawls c USING(telegram_account_id,chat_id)
        WHERE g.telegram_account_id=$1 AND g.is_active IS TRUE AND g.is_admin IS TRUE
        AND g.migrated_to IS NULL AND c.chat_id IS NULL ORDER BY g.chat_id LIMIT $2""",
            self.bot_id,
            limit,
        )
        for row in rows:
            await self.enqueue(row["chat_id"])
        return len(rows)

    async def suspend(self, chat_id, removed=False, *, reason=None):
        await self.connection.execute(
            """UPDATE group_roster_crawls SET status=$3,stopped_because=$4,
        locked_by=NULL,locked_until=NULL,available_at=now()+interval '5 minutes',updated_at=now(),
        finished_at=CASE WHEN $5 THEN now() ELSE finished_at END
        WHERE telegram_account_id=$1 AND chat_id=$2 AND status IN('queued','paused','running')""",
            self.bot_id,
            chat_id,
            "cancelled" if removed else "paused",
            reason or ("group_gone" if removed else "not_admin"),
            removed,
        )

    async def claim(self, owner, lease_seconds=120, max_concurrent=1):
        # All claimers share this account-scoped transaction lock; the cap is strict.
        await self.connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"roster:{self.bot_id}"
        )
        running = await self.connection.fetchval(
            "SELECT count(*) FROM group_roster_crawls WHERE telegram_account_id=$1 AND status='running' AND locked_until>clock_timestamp()",
            self.bot_id,
        )
        if running >= max_concurrent:
            return None
        row = await self.connection.fetchrow(
            f"""WITH due AS(SELECT c.chat_id FROM group_roster_crawls c
        JOIN bot_groups g USING(telegram_account_id,chat_id) WHERE c.telegram_account_id=$1
        AND g.is_active IS TRUE AND g.is_admin IS TRUE AND g.migrated_to IS NULL
        AND ((c.status IN ('queued','paused') AND c.available_at<=clock_timestamp()) OR
        (c.status='running' AND c.locked_until<clock_timestamp()))
        ORDER BY c.available_at,c.chat_id FOR UPDATE OF c SKIP LOCKED LIMIT 1)
        UPDATE group_roster_crawls c SET status='running',locked_by=$2,
        locked_until=clock_timestamp()+$3::float*interval '1 second',
        started_at=COALESCE(started_at,now()),updated_at=now()
        FROM due WHERE c.telegram_account_id=$1 AND c.chat_id=due.chat_id
        RETURNING {",".join("c." + f for f in RosterCrawl.__dataclass_fields__)}""",
            self.bot_id,
            owner,
            float(lease_seconds),
        )
        return RosterCrawl(**dict(row)) if row else None

    async def owned(self, crawl, owner):
        group = await self.connection.fetchval(
            """SELECT 1 FROM bot_groups WHERE telegram_account_id=$1 AND chat_id=$2
        AND is_active IS TRUE AND is_admin IS TRUE AND migrated_to IS NULL FOR UPDATE""",
            self.bot_id,
            crawl.chat_id,
        )
        if not group:
            return False
        return await self.connection.fetchval(
            """SELECT 1 FROM group_roster_crawls WHERE telegram_account_id=$1 AND chat_id=$2 AND run_id=$3 AND locked_by=$4 AND status='running' AND locked_until>clock_timestamp() FOR UPDATE""",
            self.bot_id,
            crawl.chat_id,
            crawl.run_id,
            owner,
        )

    async def fence(self):
        return await self.connection.fetchval("SELECT nextval('membership_state_revision')")

    async def valid(self, crawl, owner):
        return bool(
            await self.connection.fetchval(
                """SELECT 1 FROM group_roster_crawls c JOIN bot_groups g USING(telegram_account_id,chat_id)
        WHERE c.telegram_account_id=$1 AND c.chat_id=$2 AND c.run_id=$3 AND c.locked_by=$4 AND c.status='running'
        AND c.locked_until>clock_timestamp() AND g.is_active IS TRUE AND g.is_admin IS TRUE AND g.migrated_to IS NULL""",
                self.bot_id,
                crawl.chat_id,
                crawl.run_id,
                owner,
            )
        )

    async def advance_pass(self, crawl, owner):
        if not await self.owned(crawl, owner):
            return None
        await self.connection.execute(
            """UPDATE group_roster_crawls SET pass_number=pass_number+1,
        pass_new_count=0,filter_index=0,offset_count=0,updated_at=now()
        WHERE telegram_account_id=$1 AND chat_id=$2 AND run_id=$3 AND locked_by=$4""",
            self.bot_id,
            crawl.chat_id,
            crawl.run_id,
            owner,
        )
        return await self.get(crawl.chat_id)

    async def write_page(
        self,
        crawl,
        owner,
        users,
        skipped,
        *,
        fence,
        read_at,
        offset,
        total,
        next_filter=None,
        next_pass=False,
        lease_seconds=120,
    ):
        if not await self.owned(crawl, owner):
            return None
        seed = read_at - timedelta(days=30)
        if total is not None:
            await self.connection.execute(
                """UPDATE bot_groups SET member_count=$3,revision=revision+1,updated_at=now()
            WHERE telegram_account_id=$1 AND chat_id=$2 AND member_count IS DISTINCT FROM $3""",
                self.bot_id,
                crawl.chat_id,
                total,
            )
        identities = [user.user_id for user in users]
        if identities:
            await self.connection.execute(
                """INSERT INTO bot_users(telegram_account_id,user_id,username,first_name,last_name,account_kind)
            SELECT $1,u.id,u.username,u.first,u.last,'real' FROM unnest($2::bigint[],$3::text[],$4::text[],$5::text[]) u(id,username,first,last)
            ON CONFLICT(telegram_account_id,user_id) DO UPDATE SET
            username=COALESCE(EXCLUDED.username,bot_users.username),first_name=COALESCE(EXCLUDED.first_name,bot_users.first_name),
            last_name=COALESCE(EXCLUDED.last_name,bot_users.last_name),account_kind='real',updated_at=now()""",
                self.bot_id,
                identities,
                [u.username for u in users],
                [u.first_name for u in users],
                [u.last_name for u in users],
            )
            from repositories.postgres.user_tombstone import remove_tombstoned

            rejected = await remove_tombstoned(self.connection, self.bot_id, identities)
            if rejected:
                skipped = sorted(set(skipped) | rejected)
                identities = [identity for identity in identities if identity not in rejected]
            stored = await self.connection.fetchval(
                """WITH added AS(INSERT INTO group_memberships
            (telegram_account_id,chat_id,user_id,is_active,last_seen_at,state_changed_at,state_revision,state_event_stream)
            SELECT $1,$2,id,NOT EXISTS(SELECT 1 FROM membership_departures d WHERE d.telegram_account_id=$1 AND d.chat_id=$2 AND d.user_id=id AND d.revision>$5),$4,$6,$5,'roster' FROM unnest($3::bigint[]) id
            ON CONFLICT DO NOTHING RETURNING 1) SELECT count(*) FROM added""",
                self.bot_id,
                crawl.chat_id,
                identities,
                seed,
                fence,
                read_at,
            )
            await self.connection.execute(
                """UPDATE group_memberships SET
            state_changed_at=CASE WHEN NOT is_active THEN $5 ELSE state_changed_at END,
            state_event_stream=CASE WHEN NOT is_active THEN 'roster' ELSE state_event_stream END,
            state_event_order=CASE WHEN NOT is_active THEN 0 ELSE state_event_order END,
            is_active=TRUE,state_revision=$4,updated_at=now()
            WHERE telegram_account_id=$1 AND chat_id=$2 AND user_id=ANY($3::bigint[]) AND state_revision<=$4 AND state_changed_at<=$5 AND NOT EXISTS(SELECT 1 FROM membership_departures d WHERE d.telegram_account_id=$1 AND d.chat_id=$2 AND d.user_id=group_memberships.user_id AND d.revision>$4)""",
                self.bot_id,
                crawl.chat_id,
                identities,
                fence,
                read_at,
            )
        else:
            stored = 0
        seen_ids = identities + list(skipped)
        refused = [False] * len(identities) + [True] * len(skipped)
        promoted = await self.connection.fetchval(
            """WITH changed AS(UPDATE roster_crawl_seen SET skipped=FALSE
        WHERE telegram_account_id=$1 AND chat_id=$2 AND run_id=$3 AND skipped
        AND user_id=ANY($4::bigint[]) RETURNING 1) SELECT count(*) FROM changed""",
            self.bot_id,
            crawl.chat_id,
            crawl.run_id,
            identities,
        )
        added = await self.connection.fetchrow(
            """WITH added AS(INSERT INTO roster_crawl_seen
        (telegram_account_id,chat_id,run_id,user_id,skipped)
        SELECT $1,$2,$3,id,refused FROM unnest($4::bigint[],$5::bool[]) u(id,refused)
        ON CONFLICT DO NOTHING RETURNING skipped)
        SELECT count(*) FILTER(WHERE NOT skipped) seen,count(*) FILTER(WHERE skipped) skipped FROM added""",
            self.bot_id,
            crawl.chat_id,
            crawl.run_id,
            seen_ids,
            refused,
        )
        new, new_skipped = int(added["seen"]) + promoted, int(added["skipped"]) - promoted
        await self.connection.execute(
            """UPDATE group_roster_crawls SET offset_count=$5,filter_index=COALESCE($6,filter_index),
        pass_number=pass_number+CASE WHEN $7 THEN 1 ELSE 0 END,pass_new_count=CASE WHEN $7 THEN 0 ELSE pass_new_count+$8 END,
        seen_unique=seen_unique+$9,skipped_accounts=skipped_accounts+$10,stored_new=stored_new+$11,requests_made=requests_made+1,
        claimed_total=CASE WHEN $12::integer IS NULL THEN claimed_total ELSE GREATEST(claimed_total,$12) END,
        ambiguous_visits=0,updated_at=clock_timestamp(),locked_until=clock_timestamp()+$13::float*interval '1 second'
        WHERE telegram_account_id=$1 AND chat_id=$2 AND run_id=$3 AND locked_by=$4""",
            self.bot_id,
            crawl.chat_id,
            crawl.run_id,
            owner,
            offset,
            next_filter,
            next_pass,
            int(new),
            new,
            new_skipped,
            stored,
            total,
            float(lease_seconds),
        )
        return await self.get(crawl.chat_id)

    async def pause(self, crawl, owner, reason, seconds, *, flood=False, ambiguous=False):
        return await self.connection.fetchval(
            """UPDATE group_roster_crawls SET status='paused',stopped_because=$5,last_error=$5,
        available_at=clock_timestamp()+$6::float*interval '1 second',flood_waits=flood_waits+$7,
        ambiguous_visits=ambiguous_visits+$8,locked_by=NULL,locked_until=NULL,updated_at=now()
        WHERE telegram_account_id=$1 AND chat_id=$2 AND run_id=$3 AND locked_by=$4 AND status='running' AND locked_until>clock_timestamp() RETURNING 1""",
            self.bot_id,
            crawl.chat_id,
            crawl.run_id,
            owner,
            reason,
            float(seconds),
            int(flood),
            int(ambiguous),
        )

    async def finish(self, crawl, owner, reason, *, converged=False, failed=False):
        return await self.connection.fetchval(
            """UPDATE group_roster_crawls SET status=$5,stopped_because=$6,last_error=CASE WHEN $5='failed' THEN $6 ELSE NULL END,
        converged=$7,locked_by=NULL,locked_until=NULL,finished_at=now(),updated_at=now()
        WHERE telegram_account_id=$1 AND chat_id=$2 AND run_id=$3 AND locked_by=$4 AND status='running' AND locked_until>clock_timestamp() RETURNING 1""",
            self.bot_id,
            crawl.chat_id,
            crawl.run_id,
            owner,
            "failed" if failed else "done",
            reason,
            converged,
        )

    async def summary(self):
        row = await self.connection.fetchrow(
            """SELECT count(*) total,count(*) FILTER(WHERE status IN('queued','paused','running')) pending,
        count(*) FILTER(WHERE status='running') running,count(*) FILTER(WHERE status='done') done,count(*) FILTER(WHERE status='failed') failed,
        COALESCE(sum(seen_unique),0)::bigint seen_unique,COALESCE(sum(stored_new),0)::bigint stored_new,
        COALESCE(sum(skipped_accounts),0)::bigint skipped_accounts FROM group_roster_crawls WHERE telegram_account_id=$1""",
            self.bot_id,
        )
        return dict(row)
