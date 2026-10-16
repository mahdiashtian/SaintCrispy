"""Non-matching Couplyo channel, watch and immutable daily-stat records."""

from db.postgres.base import TableDefinition

CHANNEL = TableDefinition(
    "channels",
    """
CREATE TABLE IF NOT EXISTS channels (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 telegram_account_id BIGINT NOT NULL,channel_telegram_id BIGINT NOT NULL CHECK(channel_telegram_id<0),
 channel_link TEXT NOT NULL,channel_title TEXT,
 is_mandatory BOOLEAN NOT NULL DEFAULT FALSE,is_archive BOOLEAN NOT NULL DEFAULT FALSE,
 is_storage BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(telegram_account_id,channel_telegram_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS channels_archive ON channels(telegram_account_id) WHERE is_archive;
CREATE UNIQUE INDEX IF NOT EXISTS channels_storage ON channels(telegram_account_id) WHERE is_storage;
CREATE INDEX IF NOT EXISTS channels_mandatory ON channels(telegram_account_id,id) WHERE is_mandatory;
""",
)

WATCH = TableDefinition(
    "group_watches",
    """
CREATE TABLE IF NOT EXISTS group_watches (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 telegram_account_id BIGINT NOT NULL,chat_id BIGINT NOT NULL CHECK(chat_id<0),
 channel_telegram_id BIGINT NOT NULL CHECK(channel_telegram_id<0),channel_link TEXT NOT NULL,
 channel_title TEXT,kinds JSONB NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(kinds)='array'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(telegram_account_id,chat_id)
);
""",
)

DAILY = TableDefinition(
    "daily_stats",
    """
CREATE TABLE IF NOT EXISTS daily_stats (
 telegram_account_id BIGINT NOT NULL,stat_date DATE NOT NULL,
 new_users BIGINT NOT NULL DEFAULT 0,first_starts BIGINT NOT NULL DEFAULT 0,
 bot_active_users BIGINT,plain_starts BIGINT NOT NULL DEFAULT 0,
 referral_starts BIGINT NOT NULL DEFAULT 0,unknown_starts BIGINT NOT NULL DEFAULT 0,
 groups_added BIGINT NOT NULL DEFAULT 0,groups_removed BIGINT NOT NULL DEFAULT 0,
 groups_admin_gained BIGINT NOT NULL DEFAULT 0,groups_admin_lost BIGINT NOT NULL DEFAULT 0,
 active_senders BIGINT NOT NULL DEFAULT 0,total_users BIGINT NOT NULL DEFAULT 0,
 total_groups BIGINT NOT NULL DEFAULT 0,active_groups BIGINT NOT NULL DEFAULT 0,
 admin_groups BIGINT NOT NULL DEFAULT 0,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(telegram_account_id,stat_date)
);
""",
)

TOMBSTONE = TableDefinition(
    "user_account_tombstones",
    """
CREATE TABLE IF NOT EXISTS user_account_tombstones (
 telegram_account_id BIGINT NOT NULL,user_id BIGINT NOT NULL CHECK(user_id>0),
 account_kind TEXT NOT NULL CHECK(account_kind IN('bot','deleted')),
 observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),PRIMARY KEY(telegram_account_id,user_id)
);
""",
)

UPGRADE = """
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ;
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS start_source VARCHAR(16);
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS start_chat_id BIGINT;
ALTER TABLE membership_events ADD COLUMN IF NOT EXISTS observed_admin BOOLEAN;
CREATE INDEX IF NOT EXISTS bot_users_started_at ON bot_users(telegram_account_id,started_at) WHERE started_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS membership_messages_day ON membership_events(telegram_account_id,occurred_at,user_id) WHERE kind='message';
ALTER TABLE admin_jobs DROP CONSTRAINT IF EXISTS admin_jobs_kind_check;
ALTER TABLE admin_jobs ADD CONSTRAINT admin_jobs_kind_check CHECK(kind IN
 ('broadcast','forward','health','leave','crawl','nudge_admin','nudge_owners','refresh_users','report','purge_users'));
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS outbox_events_kind_check;
ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_kind_check CHECK(kind IN
 ('message','forward','leave','health','crawl','nudge_admin','nudge_owners','refresh_users','report','archive_document','purge_users'));
"""

BACKFILL = """
INSERT INTO channels(telegram_account_id,channel_telegram_id,channel_link,channel_title,is_mandatory,is_archive,is_storage)
SELECT telegram_account_id,(item->>'id')::bigint,COALESCE(max(item->>'link'),''),max(item->>'title'),
 bool_or(kind='forced'),bool_or(kind='archive'),bool_or(kind='storage') FROM (
 SELECT s.telegram_account_id,x.item,'forced' kind FROM app_settings s
 CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(s.value->'forced_channels')='array' THEN s.value->'forced_channels' ELSE '[]'::jsonb END) x(item) WHERE s.key='channel_management'
 UNION ALL SELECT telegram_account_id,value->'archive','archive' FROM app_settings WHERE key='channel_management' AND jsonb_typeof(value->'archive')='object'
 UNION ALL SELECT telegram_account_id,value->'storage','storage' FROM app_settings WHERE key='channel_management' AND jsonb_typeof(value->'storage')='object'
) items WHERE item->>'id' IS NOT NULL GROUP BY telegram_account_id,(item->>'id')::bigint ON CONFLICT DO NOTHING;
INSERT INTO group_watches(telegram_account_id,chat_id,channel_telegram_id,channel_link,channel_title,kinds)
SELECT s.telegram_account_id,(x->>'chat_id')::bigint,(x->'channel'->>'id')::bigint,
 COALESCE(x->'channel'->>'link',''),x->'channel'->>'title',COALESCE(x->'kinds','[]') FROM app_settings s
 CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(s.value->'watches')='array' THEN s.value->'watches' ELSE '[]'::jsonb END) x WHERE s.key='channel_management' ON CONFLICT DO NOTHING;
UPDATE app_settings SET value=value-'forced_channels'-'archive'-'storage'-'watches'
 WHERE key='channel_management';
UPDATE bot_users u SET last_active_at=x.active FROM (
 SELECT telegram_account_id,user_id,max(occurred_at) active FROM membership_events WHERE kind='message' GROUP BY telegram_account_id,user_id
) x WHERE u.telegram_account_id=x.telegram_account_id AND u.user_id=x.user_id;
"""
