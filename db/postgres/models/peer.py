"""Account-scoped group address recovery; never a membership verdict."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    "telegram_group_peers",
    """
CREATE TABLE IF NOT EXISTS telegram_group_peers (
    telegram_account_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    raw_id BIGINT NOT NULL CHECK (raw_id > 0),
    chat_kind TEXT NOT NULL CHECK (chat_kind IN ('group','supergroup')),
    access_hash BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id,chat_id),
    FOREIGN KEY (telegram_account_id,chat_id) REFERENCES bot_groups ON DELETE CASCADE,
    CHECK (chat_kind='group' OR access_hash IS NOT NULL)
);
""",
)
