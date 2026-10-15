"""Persisted rolling group quota, separate from per-user admission."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="group_request_limits",
    schema="""
CREATE TABLE IF NOT EXISTS group_request_limits (
    telegram_account_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    accepted_at TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (telegram_account_id,chat_id),
    FOREIGN KEY (telegram_account_id,chat_id)
        REFERENCES bot_groups(telegram_account_id,chat_id)
);
""",
)
