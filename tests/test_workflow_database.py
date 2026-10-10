"""Verify additive migrations and state races against a real PostgreSQL instance."""

import asyncio
import os
import uuid

import asyncpg
import pytest

from downloader_bot.bot.state.manager import ConversationManager
from downloader_bot.bot.state.menus import PersistentMenuStore
from downloader_bot.bot.state.states import ConversationState
from downloader_bot.db.postgres.engine import WORKFLOW_SCHEMA, migrate
from downloader_bot.repositories.postgres.workflow import WorkflowRepository
from downloader_bot.repositories.redis.media import FileRepository
from downloader_bot.schemas.media import Media, Quality, TelegramFile


@pytest.mark.parametrize("already_migrated", [False, True])
async def test_additive_upgrade_preserves_files_and_recovers_conversations(already_migrated):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for workflow integration tests")
    schema = "workflow_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        url, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        # A legacy database has no migration table; existing documents must survive adoption.
        from downloader_bot.db.postgres.schema import SCHEMA

        await pool.execute(SCHEMA)
        if already_migrated:
            await pool.execute(WORKFLOW_SCHEMA)
            await pool.execute("""CREATE TABLE app_schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
                INSERT INTO app_schema_migrations (version,name) VALUES
                (1,'existing_media_schema'),(2,'durable_bot_workflow');""")
        files = FileRepository(pool, None, 123)
        file = TelegramFile(1, 2, b"ref", b"peer", 3, 2048)
        await pool.execute(
            """INSERT INTO media_files
            (site,content_id,quality,telegram_account_id,document_id,access_hash,
             file_reference,origin_peer,message_id,size_bytes)
            VALUES ('youtube','video','high',123,$1,$2,$3,$4,$5,$6)""",
            file.document_id,
            file.access_hash,
            file.file_reference,
            file.origin_peer,
            file.message_id,
            file.size_bytes,
        )
        await asyncio.gather(migrate(pool), migrate(pool))
        assert await pool.fetchval("SELECT count(*) FROM app_schema_migrations") == 4
        assert await files.get("youtube", "video", "high") == file
        workflow = WorkflowRepository(pool, 123)
        manager = ConversationManager(workflow)
        await asyncio.gather(*(workflow.touch_user(10) for _ in range(30)))
        assert await pool.fetchval("SELECT count(*) FROM bot_users") == 1
        assert await pool.fetchval("SELECT is_started FROM bot_users") is False
        await workflow.touch_user(10, started=True)
        await workflow.touch_user(10)
        assert await pool.fetchval("SELECT is_started FROM bot_users") is True
        quality = Quality(
            "high", "High", "mp4", None, "mp4", "video/mp4", "progressive", "signed-secret"
        )
        media = Media(
            "youtube", "video", "Title", "", 3, "public-url", None, (quality,), "auth-secret"
        )
        menus = PersistentMenuStore(workflow, conversations=manager)
        token = await menus.create(10, 20, media, 77)
        await manager.set(10, 20, ConversationState.TRANSFERRING, transfer_id="old")
        await manager.set(10, 20, ConversationState.CHOOSING_QUALITY, menu_token=token)
        await manager.finish_transfer(10, 20, "old", ConversationState.DONE)
        assert (await manager.get(10, 20))[0] == ConversationState.CHOOSING_QUALITY
        await manager.set(11, 21, ConversationState.TRANSFERRING, transfer_id="crashed")
        other_bot = WorkflowRepository(pool, 999)
        await other_bot.set_state(11, 21, "TRANSFERRING", {"transfer_id": "other-bot"})
        assert await workflow.recover() == 1
        assert (await manager.get(11, 21))[0] == ConversationState.INTERRUPTED
        assert (await other_bot.get_state(11, 21))["state"] == "TRANSFERRING"
        recovered = await PersistentMenuStore(workflow).fetch(token, 10, 20)
        assert recovered.message_id == 77 and recovered.media.requires_refresh
        payload = await pool.fetchval("SELECT metadata::text FROM download_menus")
        assert "signed-secret" not in payload and "auth-secret" not in payload
        # A later finish writes DONE only for the operation that still owns the state.
        await manager.set(10, 20, ConversationState.TRANSFERRING, transfer_id="current")
        await manager.finish_transfer(10, 20, "current", ConversationState.DONE)
        assert await manager.get(10, 20) == (ConversationState.DONE, {})
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
