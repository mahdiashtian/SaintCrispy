"""Executable PostgreSQL model for media_catalog."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="media_catalog",
    schema="""

CREATE TABLE IF NOT EXISTS media_catalog (
    telegram_account_id BIGINT NOT NULL,
    site TEXT NOT NULL,
    content_id TEXT NOT NULL,
    metadata JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, site, content_id)
);

""",
)
