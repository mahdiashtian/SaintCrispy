"""Durable deadlines for user-facing downloaded file messages only."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="file_message_expiry",
    schema="""
CREATE TABLE IF NOT EXISTS file_message_expiry (
    telegram_account_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id INTEGER NOT NULL CHECK (message_id > 0),
    peer BYTEA NOT NULL,
    delete_at TIMESTAMPTZ NOT NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (telegram_account_id,chat_id,message_id)
);
CREATE INDEX IF NOT EXISTS file_message_expiry_due ON file_message_expiry
    (telegram_account_id,next_attempt_at) WHERE completed_at IS NULL;
CREATE INDEX IF NOT EXISTS file_message_expiry_completed ON file_message_expiry
    (telegram_account_id,completed_at) WHERE completed_at IS NOT NULL;
""",
)
