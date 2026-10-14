"""Executable PostgreSQL model for media_aliases."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="media_aliases",
    schema="""

CREATE TABLE IF NOT EXISTS media_aliases (
    telegram_account_id BIGINT NOT NULL,
    alias TEXT NOT NULL,
    site TEXT NOT NULL,
    content_id TEXT NOT NULL,
    PRIMARY KEY (telegram_account_id, alias)
);

""",
)
