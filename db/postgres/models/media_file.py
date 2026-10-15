"""Executable PostgreSQL model for media_files."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="media_files",
    schema="""

CREATE TABLE IF NOT EXISTS media_files (
    site TEXT NOT NULL,
    content_id TEXT NOT NULL,
    quality TEXT NOT NULL,
    telegram_account_id BIGINT NOT NULL,
    document_id BIGINT NOT NULL,
    access_hash BIGINT NOT NULL,
    file_reference BYTEA NOT NULL,
    origin_peer BYTEA NOT NULL,
    message_id INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site, content_id, quality, telegram_account_id)
);
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS size_bytes BIGINT;
CREATE INDEX IF NOT EXISTS media_files_origin ON media_files
    (telegram_account_id,origin_peer,message_id);

""",
)
