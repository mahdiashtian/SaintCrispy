SCHEMA = """
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
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS size_bytes BIGINT;
CREATE TABLE IF NOT EXISTS request_limits (
    telegram_account_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    scope TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (telegram_account_id, user_id, scope)
);
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
CREATE TABLE IF NOT EXISTS user_media_history (
    telegram_account_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    site TEXT NOT NULL,
    content_id TEXT NOT NULL,
    quality TEXT NOT NULL,
    delivery_count BIGINT NOT NULL DEFAULT 1,
    last_method TEXT NOT NULL,
    first_delivered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_delivered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, user_id, site, content_id, quality)
);
CREATE TABLE IF NOT EXISTS media_catalog (
    telegram_account_id BIGINT NOT NULL,
    site TEXT NOT NULL,
    content_id TEXT NOT NULL,
    metadata JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_account_id, site, content_id)
);
CREATE TABLE IF NOT EXISTS media_aliases (
    telegram_account_id BIGINT NOT NULL,
    alias TEXT NOT NULL,
    site TEXT NOT NULL,
    content_id TEXT NOT NULL,
    PRIMARY KEY (telegram_account_id, alias)
);
"""
