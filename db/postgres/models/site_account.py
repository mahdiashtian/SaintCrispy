"""Executable PostgreSQL model for site_accounts."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="site_accounts",
    schema="""

CREATE TABLE IF NOT EXISTS site_accounts (
    id BIGSERIAL PRIMARY KEY,
    site TEXT NOT NULL,
    label TEXT NOT NULL,
    credential_env TEXT NOT NULL,
    subscription TEXT NOT NULL DEFAULT 'free',
    priority INTEGER NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ,
    cooldown_until TIMESTAMPTZ,
    UNIQUE (site, label)
);

""",
)
