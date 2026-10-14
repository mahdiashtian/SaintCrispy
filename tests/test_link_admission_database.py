"""Atomic link admission/menu/history changes against a private real SQL schema."""

import asyncio
import json
import os
import uuid
from types import SimpleNamespace

import asyncpg
import pytest

from bot.state.manager import ConversationManager
from bot.state.menus import PersistentMenuStore
from core.request_context import inspection_work, user_request
from db.postgres.engine import migrate
from repositories.postgres.media import PostgresMediaRepository
from repositories.postgres.workflow import WorkflowRepository
from schemas.media import Media, Quality
from services.download import DownloadService

QUALITY = Quality("full", "Full", "h264", None, "mp4", "video/mp4", "progressive", "")
MEDIA = Media(
    "youtube", "jNQXAC9IVRw", "Test", "", 10, "https://youtu.be/jNQXAC9IVRw", None, (QUALITY,)
)


@pytest.fixture
async def database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for real admission integration")
    schema = "admission_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        url, min_size=1, max_size=8, server_settings={"search_path": schema}
    )
    try:
        await migrate(pool)
        yield pool, WorkflowRepository(pool, 123), PostgresMediaRepository(pool, 123)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_concurrent_sql_admissions_have_one_owner_and_denials_do_not_change_it(database):
    pool, workflow, _ = database
    results = await asyncio.gather(
        *(workflow.claim_link(1, 2, f"request-{i}", 60) for i in range(1000))
    )
    assert results.count(0) == 1 and all(result >= 0 for result in results)
    winner = results.index(0)
    row = await workflow.get_state(1, 2)
    assert (
        row["state"] == "INSPECTING"
        and json.loads(row["data"])["request_id"] == f"request-{winner}"
    )
    assert row["revision"] == 1
    assert await pool.fetchval("SELECT count(*) FROM bot_users") == 1


async def test_failed_admission_transaction_does_not_consume_interval_or_create_user(database):
    pool, workflow, _ = database
    with pytest.raises(asyncpg.NotNullViolationError):
        await workflow.claim_link(1, None, "invalid", 60)
    assert await pool.fetchval("SELECT count(*) FROM request_limits") == 0
    assert await pool.fetchval("SELECT count(*) FROM bot_users") == 0
    assert await workflow.claim_link(1, 2, "valid", 60) == 0


async def test_history_completion_commits_atomically_with_owned_menu(database):
    pool, workflow, files = database
    assert await workflow.claim_link(1, 2, "request", 60) == 0

    async def inspect(url):
        return MEDIA

    service = DownloadService(SimpleNamespace(inspect=inspect), files, None)
    menus = PersistentMenuStore(workflow, conversations=ConversationManager(workflow))
    with user_request(1, 2, "request"), inspection_work(True) as work:
        media = await service.inspect(MEDIA.page_url)
        assert await pool.fetchval("SELECT status FROM user_link_history") == "pending"
        token = await menus.create(1, 2, media, 10)
        assert work.completed and work.link_identity
    assert await pool.fetchval("SELECT status FROM user_link_history") == "inspected"
    row = await workflow.get_state(1, 2)
    assert row["state"] == "CHOOSING_QUALITY" and json.loads(row["data"])["menu_token"] == token
    assert await workflow.get_menu(token, 1, 2)


async def test_old_menu_completion_cannot_replace_newer_inspection_owner(database):
    pool, workflow, files = database
    await workflow.claim_link(1, 2, "old", 60)
    identity = await files.record_link_view(1, MEDIA.page_url)
    await pool.execute(
        "UPDATE request_limits SET requested_at=clock_timestamp()-interval '61 seconds'"
    )
    assert await workflow.claim_link(1, 2, "new", 60) == 0
    await workflow.save_menu_and_choose(
        "old-menu", 1, 2, MEDIA, link_identity=identity, request_id="old"
    )
    await workflow.finish_inspection(1, 2, "old", link_identity=identity)
    state = await workflow.get_state(1, 2)
    assert state["state"] == "INSPECTING" and json.loads(state["data"])["request_id"] == "new"


async def test_failed_menu_write_retains_pending_history_for_failure_cleanup(database):
    pool, workflow, files = database
    await workflow.claim_link(1, 2, "request", 60)
    identity = await files.record_link_view(1, MEDIA.page_url)
    with pytest.raises(asyncpg.NotNullViolationError):
        await workflow.save_menu_and_choose(
            None, 1, 2, MEDIA, link_identity=identity, request_id="request"
        )
    assert await pool.fetchval("SELECT count(*) FROM download_menus") == 0
    assert await pool.fetchval("SELECT status FROM user_link_history") == "pending"
    await workflow.finish_inspection(1, 2, "request", link_identity=identity)
    assert await pool.fetchval("SELECT status FROM user_link_history") == "failed"
    assert (await workflow.get_state(1, 2))["state"] == "FAILED"
