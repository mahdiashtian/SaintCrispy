"""Resumable per-account roster state and durable per-run deduplication."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    "group_roster_crawls",
    """
CREATE TABLE IF NOT EXISTS group_roster_crawls (
 telegram_account_id BIGINT NOT NULL, chat_id BIGINT NOT NULL CHECK(chat_id<0),
 run_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued'
 CHECK(status IN ('queued','running','paused','done','failed','cancelled')),
 pass_number INTEGER NOT NULL DEFAULT 1, filter_index INTEGER NOT NULL DEFAULT 0,
 offset_count INTEGER NOT NULL DEFAULT 0, pass_new_count INTEGER NOT NULL DEFAULT 0,
 claimed_total INTEGER, seen_unique INTEGER NOT NULL DEFAULT 0,
 stored_new INTEGER NOT NULL DEFAULT 0, skipped_accounts INTEGER NOT NULL DEFAULT 0,
 requests_made INTEGER NOT NULL DEFAULT 0, flood_waits INTEGER NOT NULL DEFAULT 0,
 ambiguous_visits INTEGER NOT NULL DEFAULT 0, converged BOOLEAN NOT NULL DEFAULT FALSE,
 stopped_because TEXT, last_error TEXT, locked_by TEXT, locked_until TIMESTAMPTZ,
 available_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(telegram_account_id,chat_id),
 FOREIGN KEY(telegram_account_id,chat_id) REFERENCES bot_groups ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS roster_crawls_due ON group_roster_crawls
 (telegram_account_id,available_at,chat_id) WHERE status IN ('queued','paused','running');
""",
)

SEEN = TableDefinition(
    "roster_crawl_seen",
    """
CREATE TABLE IF NOT EXISTS roster_crawl_seen (
 telegram_account_id BIGINT NOT NULL, chat_id BIGINT NOT NULL, run_id TEXT NOT NULL,
 user_id BIGINT NOT NULL CHECK(user_id>0), skipped BOOLEAN NOT NULL,
 PRIMARY KEY(telegram_account_id,chat_id,run_id,user_id),
 FOREIGN KEY(telegram_account_id,chat_id) REFERENCES group_roster_crawls ON DELETE CASCADE
);
""",
)

DEPARTURES = TableDefinition(
    "membership_departures",
    """
CREATE TABLE IF NOT EXISTS membership_departures (
 telegram_account_id BIGINT NOT NULL,chat_id BIGINT NOT NULL,user_id BIGINT NOT NULL CHECK(user_id>0),
 revision BIGINT NOT NULL,occurred_at TIMESTAMPTZ NOT NULL,event_key TEXT NOT NULL,
 PRIMARY KEY(telegram_account_id,chat_id,user_id),
 FOREIGN KEY(telegram_account_id,chat_id) REFERENCES bot_groups ON DELETE CASCADE
);
""",
)

UPGRADE = """
CREATE SEQUENCE IF NOT EXISTS membership_state_revision;
ALTER TABLE group_memberships ADD COLUMN IF NOT EXISTS state_revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE group_memberships ADD COLUMN IF NOT EXISTS msg_week INTEGER NOT NULL DEFAULT 0;
ALTER TABLE group_memberships ADD COLUMN IF NOT EXISTS msg_this_week INTEGER NOT NULL DEFAULT 0;
ALTER TABLE group_memberships ADD COLUMN IF NOT EXISTS msg_last_week INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS membership_messages_group_week ON membership_events
 (telegram_account_id,chat_id,occurred_at) INCLUDE(user_id) WHERE kind='message';
ALTER TABLE admin_jobs DROP CONSTRAINT IF EXISTS admin_jobs_kind_check;
ALTER TABLE admin_jobs ADD CONSTRAINT admin_jobs_kind_check
 CHECK(kind IN ('broadcast','forward','health','leave','crawl','nudge_admin','nudge_owners','refresh_users','report'));
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS outbox_events_kind_check;
ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_kind_check
 CHECK(kind IN ('message','forward','leave','health','crawl','nudge_admin','nudge_owners','refresh_users','report','archive_document'));
"""
