"""Bounded rolling request windows; old rate-limit history is adopted on upgrade."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    "download_request_windows",
    """
CREATE TABLE IF NOT EXISTS download_request_windows (
    telegram_account_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL CHECK (user_id > 0),
    scope TEXT NOT NULL CHECK (scope IN ('inspect','download')),
    accepted_at TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (telegram_account_id,user_id,scope),
    CHECK (cardinality(accepted_at) <= 1000)
);
""",
)

UPGRADE = (
    MODEL.schema
    + """
INSERT INTO download_request_windows (telegram_account_id,user_id,scope,accepted_at)
SELECT telegram_account_id,user_id,scope,ARRAY[requested_at] FROM request_limits
WHERE user_id > 0 AND scope IN ('inspect','download')
ON CONFLICT DO NOTHING;
"""
)
