"""Durable replay-safe lifecycle audit, including stale evidence."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="group_events",
    schema="""
CREATE TABLE IF NOT EXISTS group_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_account_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    event_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor_id BIGINT CHECK (actor_id > 0),
    occurred_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied BOOLEAN NOT NULL,
    previous_active BOOLEAN,
    previous_admin BOOLEAN,
    is_active BOOLEAN,
    is_admin BOOLEAN,
    can_send BOOLEAN,
    UNIQUE (telegram_account_id,chat_id,event_key),
    FOREIGN KEY (telegram_account_id,chat_id)
        REFERENCES bot_groups(telegram_account_id,chat_id)
);
CREATE INDEX IF NOT EXISTS group_events_history ON group_events
    (telegram_account_id,chat_id,occurred_at DESC,id DESC);
""",
)
