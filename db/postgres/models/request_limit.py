"""Executable PostgreSQL model for request_limits."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="request_limits",
    schema="""

CREATE TABLE IF NOT EXISTS request_limits (
    telegram_account_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    scope TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (telegram_account_id, user_id, scope)
);

""",
)
