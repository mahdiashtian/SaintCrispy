"""Reference-layout behavior: transactions, scoped FSM and update isolation."""

import asyncio
import os
import threading
import uuid
from types import SimpleNamespace

import asyncpg
import pytest

from bot.handlers.dispatcher import Dispatcher
from bot.session import create_session
from bot.state.manager import ConversationManager
from bot.state.storage import MemoryStateStorage, PostgresStateStorage
from core.enums import ConversationState
from db.postgres.engine import migrate
from db.postgres.models import TABLES
from db.redis.pool import create_redis
from repositories.postgres.unit_of_work import PostgresUnitOfWork
from repositories.postgres.workflow import WorkflowRepository
from services.user import UserService


@pytest.fixture
async def database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Use a dedicated PostgreSQL database")
    schema = "structure_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        url, min_size=1, max_size=8, server_settings={"search_path": schema}
    )
    try:
        await migrate(pool)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_unit_of_work_commits_user_and_state_together(database):
    async with PostgresUnitOfWork(database, 123) as uow:
        await uow.workflow.touch_user(1, started=True)
        await uow.workflow.set_state(1, 2, "MAIN", {})
        assert await database.fetchval("SELECT count(*) FROM bot_users") == 0
    assert await database.fetchval("SELECT count(*) FROM bot_users") == 1
    assert await database.fetchval("SELECT count(*) FROM bot_conversations") == 1


async def test_unit_of_work_rolls_back_all_changes_after_failure(database):
    with pytest.raises(RuntimeError):
        async with PostgresUnitOfWork(database, 123) as uow:
            await uow.workflow.touch_user(1)
            await uow.workflow.set_state(1, 2, "MAIN", {})
            raise RuntimeError("Test operation failed")
    assert await database.fetchval("SELECT count(*) FROM bot_users") == 0
    assert await database.fetchval("SELECT count(*) FROM bot_conversations") == 0


async def test_cancelled_unit_of_work_rolls_back_and_releases_connection(database):
    started = asyncio.Event()

    async def operation():
        async with PostgresUnitOfWork(database, 123) as uow:
            await uow.workflow.touch_user(1)
            started.set()
            await asyncio.Future()

    task = asyncio.create_task(operation())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await database.fetchval("SELECT count(*) FROM bot_users") == 0
    assert database.get_idle_size() == database.get_size()


async def test_factory_operations_never_share_a_transaction(database):
    def factory():
        return PostgresUnitOfWork(database, 123)

    async with factory() as first, factory() as second:
        with pytest.raises(RuntimeError):
            await first.__aenter__()
        assert first.workflow.pool is first.media.pool
        assert first.workflow.pool is not second.workflow.pool
        await first.workflow.touch_user(1)
        await second.workflow.touch_user(2)
    assert await database.fetchval("SELECT count(*) FROM bot_users") == 2


async def test_transactional_user_start_sets_both_records(database):
    workflow = WorkflowRepository(database, 123)
    service = UserService(workflow, lambda: PostgresUnitOfWork(database, 123))
    assert await service.start(1, 2)
    assert await database.fetchval("SELECT is_started FROM bot_users")
    assert (await workflow.get_state(1, 2))["state"] == "MAIN"


async def test_concurrent_state_patches_are_atomic_and_preserve_operation_owner(database):
    workflow = WorkflowRepository(database, 123)
    manager = ConversationManager(PostgresStateStorage(workflow))
    await manager.set_state(1, 2, ConversationState.TRANSFERRING, transfer_id="owner")
    await asyncio.gather(*(manager.update_data(1, 2, **{f"key_{i}": i}) for i in range(100)))
    state, data = await manager.get(1, 2)
    assert state is ConversationState.TRANSFERRING
    assert data == {"transfer_id": "owner", **{f"key_{i}": i for i in range(100)}}
    assert (await workflow.get_state(1, 2))["revision"] == 101


async def test_model_registry_matches_migrated_schema(database):
    rows = await database.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()"
    )
    assert {row["table_name"] for row in rows} == {table.name for table in TABLES} | {
        "app_schema_migrations"
    }


async def test_generated_reference_builds_the_same_empty_database_schema(database):
    from db.postgres.schema import reference_schema

    # A private temporary schema, never a production installation or its data.
    async with database.acquire() as connection, connection.transaction():
        before = await connection.fetch(
            """SELECT table_name,column_name,data_type,is_nullable,column_default
            FROM information_schema.columns WHERE table_schema=current_schema()
            ORDER BY table_name,ordinal_position"""
        )
        schema = "reference_test_" + uuid.uuid4().hex
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET LOCAL search_path="{schema}"')
        await connection.execute(reference_schema())
        after = await connection.fetch(
            """SELECT table_name,column_name,data_type,is_nullable,column_default
            FROM information_schema.columns WHERE table_schema=current_schema()
            ORDER BY table_name,ordinal_position"""
        )

        # serial defaults embed the search-path schema; compare their structure.
        def normalize(rows):
            return [
                (
                    r["table_name"],
                    r["column_name"],
                    r["data_type"],
                    r["is_nullable"],
                    "sequence"
                    if "nextval(" in (r["column_default"] or "")
                    else r["column_default"],
                )
                for r in rows
            ]

        assert normalize(before) == normalize(after)
        await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def test_memory_fsm_scopes_chats_and_does_not_expose_mutable_storage():
    storage = MemoryStateStorage()
    manager = ConversationManager(storage)
    await manager.set_state(1, 2, ConversationState.INSPECTING, request_id="first")
    await manager.set_state(1, 3, ConversationState.MAIN)
    data = await manager.get_data(1, 2)
    data["request_id"] = "changed"
    assert await manager.get_data(1, 2) == {"request_id": "first"}
    await manager.update_data(1, 2, other=True)
    assert (await manager.get_data(1, 2))["other"]
    assert await manager.get_state(1, 3) is ConversationState.MAIN
    await manager.finish(1, 2)
    assert await manager.get(1, 2) == (ConversationState.MAIN, {})


async def test_malformed_state_returns_main_but_retains_original_payload():
    storage = MemoryStateStorage()
    await storage.set_state(1, 2, "TRANSFERRING", "malformed")
    manager = ConversationManager(storage)
    assert await manager.get(1, 2) == (ConversationState.MAIN, {})
    assert (await storage.get_state(1, 2))["data"] == "malformed"


async def test_dispatch_failure_is_isolated_from_the_next_update(caplog):
    callbacks, calls = [], []
    dispatcher = Dispatcher(SimpleNamespace(add_event_handler=lambda cb, _: callbacks.append(cb)))

    async def handler(event):
        if event.id == 1:
            raise RuntimeError("PRIVATE-FAILURE-MESSAGE")
        calls.append(event.id)

    dispatcher.add_event_handler(handler, None)
    for identity in (1, 2):
        await callbacks[0](SimpleNamespace(sender_id=1, chat_id=-2, id=identity))
    assert calls == [2] and "PRIVATE-FAILURE-MESSAGE" not in caplog.text


@pytest.mark.parametrize("sender", [None, -10, 0, True])
async def test_dispatch_rejects_anonymous_or_channel_identity_before_side_effects(sender):
    callbacks = []
    dispatcher = Dispatcher(SimpleNamespace(add_event_handler=lambda cb, _: callbacks.append(cb)))

    async def handler(event):
        pytest.fail("Invalid user identity reached the domain")

    dispatcher.add_event_handler(handler, None)
    await callbacks[0](SimpleNamespace(sender_id=sender, chat_id=-2))


async def test_dispatch_does_not_swallow_shutdown_cancellation():
    callbacks = []
    dispatcher = Dispatcher(SimpleNamespace(add_event_handler=lambda cb, _: callbacks.append(cb)))

    async def handler(event):
        raise asyncio.CancelledError

    dispatcher.add_event_handler(handler, None)
    with pytest.raises(asyncio.CancelledError):
        await callbacks[0](SimpleNamespace(sender_id=1, chat_id=2))


async def test_session_construction_and_close_are_off_loop(tmp_path, monkeypatch):
    thread_ids = []

    def construct(path):
        thread_ids.append(threading.get_ident())
        return SimpleNamespace(close=lambda: thread_ids.append(threading.get_ident()))

    monkeypatch.setattr("bot.session.SQLiteSession", construct)
    async with create_session(tmp_path / "session"):
        await asyncio.sleep(0)
    assert len(thread_ids) == 2
    assert all(identity != threading.get_ident() for identity in thread_ids)


async def test_real_sqlite_session_reopens_after_off_loop_close(tmp_path):
    path = tmp_path / "native-session"
    async with create_session(path) as session:
        session.set_dc(2, "127.0.0.1", 443)
        await asyncio.to_thread(session.save)
    async with create_session(path) as reopened:
        assert reopened.dc_id == 2 and reopened.server_address == "127.0.0.1"


async def test_cancelled_session_open_still_closes_created_session(tmp_path, monkeypatch):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def construct(path):
        entered.set()
        release.wait(timeout=5)
        return SimpleNamespace(close=closed.set)

    async def operation():
        async with create_session(tmp_path / "session"):
            pytest.fail("Cancelled startup should not continue")

    monkeypatch.setattr("bot.session.SQLiteSession", construct)
    task = asyncio.create_task(operation())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_redis_client_owns_and_closes_its_bounded_pool():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("Use a dedicated Redis instance")
    client = create_redis(url)
    try:
        assert client.auto_close_connection_pool
        await client.ping()
        pool = client.connection_pool
        assert pool.max_connections == 16
    finally:
        await client.aclose()
    assert all(not connection.is_connected for connection in pool._available_connections)
