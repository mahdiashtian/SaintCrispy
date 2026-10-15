"""Bot-scoped group snapshots; unknown standing remains nullable."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="bot_groups",
    schema="""
CREATE TABLE IF NOT EXISTS bot_groups (
    telegram_account_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL CHECK (chat_id < 0),
    chat_kind TEXT NOT NULL CHECK (chat_kind IN ('group','supergroup')),
    title TEXT,
    username TEXT,
    is_active BOOLEAN,
    is_admin BOOLEAN,
    can_send BOOLEAN,
    added_by_user_id BIGINT CHECK (added_by_user_id > 0),
    added_at TIMESTAMPTZ,
    readded_at TIMESTAMPTZ,
    removed_at TIMESTAMPTZ,
    admin_since TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_event_at TIMESTAMPTZ NOT NULL DEFAULT 'epoch',
    last_event_priority INTEGER NOT NULL DEFAULT 0,
    event_cursors JSONB NOT NULL DEFAULT '{}',
    migrated_to BIGINT CHECK (migrated_to < 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id,chat_id)
);
CREATE INDEX IF NOT EXISTS bot_groups_active ON bot_groups
    (telegram_account_id,chat_id) WHERE is_active;
""",
)
