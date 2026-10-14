"""Executable PostgreSQL model for user_media_history."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="user_media_history",
    schema="""

CREATE TABLE IF NOT EXISTS user_media_history (
    telegram_account_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    site TEXT NOT NULL,
    content_id TEXT NOT NULL,
    quality TEXT NOT NULL,
    delivery_count BIGINT NOT NULL DEFAULT 1,
    last_method TEXT NOT NULL,
    first_delivered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_delivered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, user_id, site, content_id, quality)
);

""",
)
