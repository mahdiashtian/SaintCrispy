"""Operational tables with explicit ownership, leases and audit history."""

from db.postgres.base import TableDefinition

SETTING = TableDefinition(
    "app_settings",
    """
CREATE TABLE IF NOT EXISTS app_settings (
    telegram_account_id BIGINT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id,key)
);
""",
)

AUDIT = TableDefinition(
    "admin_actions",
    """
CREATE TABLE IF NOT EXISTS admin_actions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_account_id BIGINT NOT NULL,
    actor_id BIGINT NOT NULL CHECK (actor_id > 0),
    action TEXT NOT NULL,
    target_id BIGINT,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS admin_actions_history ON admin_actions
    (telegram_account_id,id DESC);
""",
)

REQUEST = TableDefinition(
    "admin_requests",
    """
CREATE TABLE IF NOT EXISTS admin_requests (
    telegram_account_id BIGINT NOT NULL,
    token TEXT NOT NULL,
    owner_id BIGINT NOT NULL CHECK (owner_id > 0),
    kind TEXT NOT NULL CHECK (kind IN ('prompt','confirm')),
    action TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    message_id BIGINT,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '20 minutes',
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id,token)
);
CREATE INDEX IF NOT EXISTS admin_requests_reply ON admin_requests
    (telegram_account_id,owner_id,message_id) WHERE consumed_at IS NULL;
""",
)

JOB = TableDefinition(
    "admin_jobs",
    """
CREATE TABLE IF NOT EXISTS admin_jobs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_account_id BIGINT NOT NULL,
    requested_by BIGINT NOT NULL CHECK (requested_by > 0),
    kind TEXT NOT NULL CHECK (kind IN ('broadcast','forward','health','leave')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','done','failed','cancelled')),
    dedupe_key TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    cursor BIGINT,
    total_count BIGINT NOT NULL DEFAULT 0,
    success_count BIGINT NOT NULL DEFAULT 0,
    failure_count BIGINT NOT NULL DEFAULT 0,
    skipped_count BIGINT NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_until TIMESTAMPTZ,
    locked_by TEXT,
    last_error TEXT,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (telegram_account_id,dedupe_key)
);
CREATE INDEX IF NOT EXISTS admin_jobs_claim ON admin_jobs
    (telegram_account_id,available_at,id) WHERE status IN ('queued','running');
""",
)

OUTBOX = TableDefinition(
    "outbox_events",
    """
CREATE TABLE IF NOT EXISTS outbox_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_account_id BIGINT NOT NULL,
    dedupe_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('message','forward','leave','health')),
    target_id BIGINT NOT NULL CHECK (target_id <> 0),
    payload JSONB NOT NULL,
    random_id BIGINT NOT NULL,
    job_id BIGINT REFERENCES admin_jobs(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','leased','sent','dead','cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_until TIMESTAMPTZ,
    locked_by TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (telegram_account_id,dedupe_key)
);
CREATE INDEX IF NOT EXISTS outbox_events_claim ON outbox_events
    (telegram_account_id,available_at,id) WHERE status IN ('pending','leased');
CREATE INDEX IF NOT EXISTS outbox_job ON outbox_events(job_id,status);
""",
)

UPGRADE = """
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS first_name TEXT;
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_name TEXT;
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS account_kind TEXT NOT NULL DEFAULT 'unknown'
    CHECK (account_kind IN ('real','bot','deleted','unknown'));
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS owner_id BIGINT CHECK (owner_id > 0);
ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS admin_ids JSONB NOT NULL DEFAULT '[]';
ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS member_count INTEGER CHECK (member_count >= 0);
ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS link TEXT;
ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ;
ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE bot_groups ADD COLUMN IF NOT EXISTS links_per_minute INTEGER
    CHECK (links_per_minute BETWEEN 1 AND 1000);
CREATE INDEX IF NOT EXISTS bot_groups_admin_page ON bot_groups
    (telegram_account_id,is_active,chat_id);
CREATE INDEX IF NOT EXISTS bot_groups_adder ON bot_groups(telegram_account_id,added_by_user_id);
CREATE INDEX IF NOT EXISTS bot_users_admin_page ON bot_users(telegram_account_id,user_id);
CREATE INDEX IF NOT EXISTS bot_users_username ON bot_users(telegram_account_id,lower(username));
"""
