"""Executable PostgreSQL model for download_menus."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="download_menus",
    schema="""

CREATE TABLE IF NOT EXISTS download_menus (
    telegram_account_id BIGINT NOT NULL,
    token TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id INTEGER,
    metadata JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, token)
);
CREATE INDEX IF NOT EXISTS download_menus_owner
    ON download_menus (telegram_account_id, user_id, chat_id, created_at);

""",
)
