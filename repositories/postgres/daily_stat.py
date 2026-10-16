"""Tehran-day activity and immutable nightly source-compatible snapshots."""

from datetime import date


class DailyStatsRepository:
    def __init__(self, connection, bot_id):
        self.connection, self.bot_id = connection, bot_id

    async def snapshot(self, day):
        if type(day) is not date:
            raise ValueError("A calendar day is required")
        row = await self.connection.fetchrow(
            """WITH bounds AS (
        SELECT $2::date::timestamp AT TIME ZONE 'Asia/Tehran' lo,($2::date+1)::timestamp AT TIME ZONE 'Asia/Tehran' hi),
        users AS(SELECT count(*) total_users,count(*) FILTER(WHERE first_seen_at>=lo AND first_seen_at<hi) new_users,
        count(*) FILTER(WHERE started_at>=lo AND started_at<hi) first_starts,
        count(*) FILTER(WHERE started_at>=lo AND started_at<hi AND start_source='plain') plain_starts,
        count(*) FILTER(WHERE started_at>=lo AND started_at<hi AND start_source='referral') referral_starts,
        count(*) FILTER(WHERE started_at>=lo AND started_at<hi AND (start_source IS NULL OR start_source NOT IN('plain','referral'))) unknown_starts,
        count(*) FILTER(WHERE is_started AND NOT is_blocked) bot_active_users FROM bot_users,bounds WHERE telegram_account_id=$1),
        groups AS(SELECT count(*) total_groups,count(*) FILTER(WHERE is_active IS TRUE) active_groups,
        count(*) FILTER(WHERE is_active IS TRUE AND is_admin IS TRUE) admin_groups FROM bot_groups WHERE telegram_account_id=$1),
        churn AS(SELECT count(DISTINCT chat_id) FILTER(WHERE is_active IS TRUE AND previous_active IS DISTINCT FROM TRUE) groups_added,
        count(DISTINCT chat_id) FILTER(WHERE is_active IS FALSE AND previous_active IS TRUE) groups_removed,
        count(DISTINCT chat_id) FILTER(WHERE is_admin IS TRUE AND previous_admin IS DISTINCT FROM TRUE) groups_admin_gained,
        count(DISTINCT chat_id) FILTER(WHERE is_admin IS FALSE AND previous_admin IS TRUE) groups_admin_lost
        FROM group_events,bounds WHERE telegram_account_id=$1 AND applied AND occurred_at>=lo AND occurred_at<hi),
        activity AS(SELECT count(DISTINCT e.user_id) active_senders FROM membership_events e,bounds
        WHERE e.telegram_account_id=$1 AND e.kind='message' AND e.occurred_at>=lo AND e.occurred_at<hi AND e.observed_admin IS TRUE)
        SELECT $2::date stat_date,* FROM users,groups,churn,activity""",
            self.bot_id,
            day,
        )
        return dict(row)

    async def save(self, day):
        values = await self.snapshot(day)
        fields = tuple(values)
        return await self.connection.fetchval(
            f"INSERT INTO daily_stats(telegram_account_id,{','.join(fields)}) VALUES($1,{','.join('$' + str(i) for i in range(2, len(fields) + 2))}) ON CONFLICT DO NOTHING RETURNING stat_date",
            self.bot_id,
            *(values[field] for field in fields),
        )

    async def history(self, limit=14, offset=0):
        rows = await self.connection.fetch(
            "SELECT * FROM daily_stats WHERE telegram_account_id=$1 ORDER BY stat_date DESC LIMIT $2 OFFSET $3",
            self.bot_id,
            max(1, min(31, limit)),
            max(0, min(100000, offset)),
        )
        return tuple(dict(row) for row in rows)
