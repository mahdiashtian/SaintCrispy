"""Executable PostgreSQL model for bot_conversations."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="bot_conversations",
    schema="""

CREATE TABLE IF NOT EXISTS bot_conversations (
    telegram_account_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    state TEXT NOT NULL,
    data JSONB NOT NULL DEFAULT '{}',
    revision BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, user_id, chat_id)
);

""",
)
