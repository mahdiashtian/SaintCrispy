"""Executable PostgreSQL model for user_link_history."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="user_link_history",
    schema="""

CREATE TABLE IF NOT EXISTS user_link_history (
    telegram_account_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    url_hash TEXT NOT NULL,
    input_url TEXT NOT NULL,
    site TEXT,
    content_id TEXT,
    canonical_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    view_count BIGINT NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, user_id, url_hash)
);
CREATE INDEX IF NOT EXISTS user_link_history_content
    ON user_link_history (telegram_account_id, site, content_id);

""",
)
