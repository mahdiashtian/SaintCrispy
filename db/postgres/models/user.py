"""Executable PostgreSQL model for bot_users."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="bot_users",
    schema="""

CREATE TABLE IF NOT EXISTS bot_users (
    telegram_account_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    is_started BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, user_id)
);

""",
)
