"""Bot-scoped group snapshots; unknown standing remains nullable."""

from db.postgres.base import TableDefinition

MODEL = TableDefinition(
    name="bot_groups",
    schema="""
CREATE TABLE IF NOT EXISTS bot_groups (
    telegram_account_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL CHECK (chat_id < 0),
    chat_kind TEXT NOT NULL CHECK (chat_kind IN ('group','supergroup')),
    title TEXT,
    username TEXT,
    is_active BOOLEAN,
    is_admin BOOLEAN,
    can_send BOOLEAN,
    added_by_user_id BIGINT CHECK (added_by_user_id > 0),
    added_at TIMESTAMPTZ,
    readded_at TIMESTAMPTZ,
    removed_at TIMESTAMPTZ,
    admin_since TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_event_at TIMESTAMPTZ NOT NULL DEFAULT 'epoch',
    last_event_priority INTEGER NOT NULL DEFAULT 0,
    event_cursors JSONB NOT NULL DEFAULT '{}',
    migrated_to BIGINT CHECK (migrated_to < 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id,chat_id)
);
CREATE INDEX IF NOT EXISTS bot_groups_active ON bot_groups
    (telegram_account_id,chat_id) WHERE is_active;
""",
)

# Applied by a new revision, without changing the historical creation migration.
INVARIANTS_SCHEMA = """
UPDATE bot_groups SET admin_since=NULL
WHERE is_admin IS NOT TRUE AND admin_since IS NOT NULL;
WITH RECURSIVE lineage AS (
    SELECT telegram_account_id,chat_id,chat_id AS origin_id,ARRAY[chat_id] AS visited
    FROM bot_groups
    UNION ALL
    SELECT target.telegram_account_id,target.chat_id,parent.chat_id,
        array_append(target.visited,parent.chat_id)
    FROM lineage target JOIN bot_groups parent
        ON parent.telegram_account_id=target.telegram_account_id
        AND parent.migrated_to=target.origin_id
    WHERE NOT parent.chat_id=ANY(target.visited)
), first_add AS (
    SELECT DISTINCT ON (target.telegram_account_id,target.chat_id)
        target.telegram_account_id,target.chat_id,event.actor_id
    FROM lineage target JOIN group_events event
        ON event.telegram_account_id=target.telegram_account_id
        AND event.chat_id=target.origin_id
    WHERE event.applied AND event.kind IN ('added','readded') AND event.actor_id IS NOT NULL
    ORDER BY target.telegram_account_id,target.chat_id,event.occurred_at,event.id
)
UPDATE bot_groups target SET added_by_user_id=first_add.actor_id
FROM first_add
WHERE target.telegram_account_id=first_add.telegram_account_id
    AND target.chat_id=first_add.chat_id
    AND target.added_by_user_id IS DISTINCT FROM first_add.actor_id;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='bot_groups'::regclass AND conname='bot_groups_admin_since_standing'
    ) THEN
        ALTER TABLE bot_groups ADD CONSTRAINT bot_groups_admin_since_standing
            CHECK (is_admin IS TRUE OR admin_since IS NULL);
    END IF;
END $$;
"""
