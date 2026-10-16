"""Human membership, ordered evidence and replay-safe activity accounting."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    "group_memberships",
    """
CREATE TABLE IF NOT EXISTS group_memberships (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_account_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL CHECK (chat_id < 0),
    user_id BIGINT NOT NULL CHECK (user_id > 0),
    is_active BOOLEAN NOT NULL,
    last_seen_at TIMESTAMPTZ,
    state_changed_at TIMESTAMPTZ NOT NULL,
    state_event_order BIGINT NOT NULL DEFAULT 0,
    state_event_stream TEXT NOT NULL DEFAULT 'message',
    message_count BIGINT NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (telegram_account_id,chat_id,user_id),
    FOREIGN KEY (telegram_account_id,chat_id) REFERENCES bot_groups ON DELETE CASCADE,
    FOREIGN KEY (telegram_account_id,user_id) REFERENCES bot_users ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memberships_active ON group_memberships
    (telegram_account_id,chat_id,user_id) WHERE is_active;
CREATE INDEX IF NOT EXISTS memberships_last_seen ON group_memberships
    (telegram_account_id,chat_id,last_seen_at DESC,user_id);
""",
)

EVENT = TableDefinition(
    "membership_events",
    """
CREATE TABLE IF NOT EXISTS membership_events (
    telegram_account_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    event_key TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('message','joined','left')),
    occurred_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id,chat_id,event_key),
    FOREIGN KEY (telegram_account_id,chat_id,user_id)
        REFERENCES group_memberships(telegram_account_id,chat_id,user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS membership_events_daily ON membership_events
    (telegram_account_id,occurred_at,kind);
""",
)
