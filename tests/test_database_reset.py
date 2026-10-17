"""Destructive feature checks use an isolated schema and dedicated SQL/Redis services."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from test_admin_reference_database import app as app
from test_couplyo_recovery import resources
from test_download_settings import event

from bot.handlers import admin as ui
from bot.handlers.admin_maintenance import reset_callback
from bot.runtime import run_with_recovery
from core.maintenance import MaintenanceGate
from db.postgres.engine import migrate
from repositories.postgres.database_reset import RESET_LOCK, TABLE_NAMES, DatabaseResetRepository
from repositories.redis.database_reset import ResetRedis
from schemas.media import DownloadError
from services.database_reset import DatabaseReset, recover_reset


@pytest.fixture
async def reset(app):
    async with app.pool.acquire() as connection:
        repository = DatabaseResetRepository(connection, app.bot)
        await repository.join_runtime()
        await connection.execute("SELECT pg_advisory_lock($1::bigint)", app.bot)
        gate, deps = MaintenanceGate(), resources()
        gate.ready = True

        @asynccontextmanager
        async def guard():
            async with gate.guard(**deps):
                yield

        service = DatabaseReset(
            app.admin,
            repository,
            ResetRedis(app.redis, app.redis),
            guard=guard,
            seal=gate.seal,
            shutdown=AsyncMock(),
        )
        app.admin.maintenance.database_reset = service
        yield SimpleNamespace(service=service, repository=repository, gate=gate, deps=deps)


async def seed_all_tables(app):
    statements = [
        "INSERT INTO bot_users(telegram_account_id,user_id,is_admin) VALUES($1,7,true)",
        "INSERT INTO media_files(site,content_id,quality,telegram_account_id,document_id,access_hash,file_reference,origin_peer,message_id) VALUES('test','id','original',$1,1,1,'a','b',1)",
        "INSERT INTO request_limits VALUES($1,7,'download',now())",
        "INSERT INTO user_link_history(telegram_account_id,user_id,url_hash,input_url) VALUES($1,7,'hash','https://example.invalid/media')",
        "INSERT INTO user_media_history(telegram_account_id,user_id,site,content_id,quality,last_method) VALUES($1,7,'test','id','original','cached')",
        "INSERT INTO media_catalog(telegram_account_id,site,content_id,metadata) VALUES($1,'test','id','{}')",
        "INSERT INTO media_aliases VALUES($1,'alias','test','id')",
        "INSERT INTO bot_conversations(telegram_account_id,user_id,chat_id,state) VALUES($1,7,7,'MAIN')",
        "INSERT INTO download_menus(telegram_account_id,token,user_id,chat_id,metadata) VALUES($1,'menu',7,7,'{}')",
        "INSERT INTO group_request_limits VALUES($1,-10,ARRAY[now()])",
        "INSERT INTO file_message_expiry(telegram_account_id,chat_id,message_id,peer,delete_at,next_attempt_at) VALUES($1,7,1,'peer',now(),now())",
        "INSERT INTO group_memberships(telegram_account_id,chat_id,user_id,is_active,state_changed_at) VALUES($1,-10,7,true,now())",
        "INSERT INTO membership_events(telegram_account_id,chat_id,event_key,user_id,kind,occurred_at) VALUES($1,-10,'message',7,'message',now())",
        "INSERT INTO app_settings(telegram_account_id,key,value) VALUES($1,'user_links_per_minute','3')",
        "INSERT INTO admin_actions(telegram_account_id,actor_id,action) VALUES($1,999,'test')",
        "INSERT INTO admin_requests(telegram_account_id,token,owner_id,kind,action) VALUES($1,'old',999,'confirm','leave')",
        "INSERT INTO admin_jobs(telegram_account_id,requested_by,kind,dedupe_key) VALUES($1,999,'health','job')",
        "INSERT INTO outbox_events(telegram_account_id,dedupe_key,kind,target_id,payload,random_id) VALUES($1,'outbox','health',-10,'{}',1)",
        "INSERT INTO telegram_group_peers(telegram_account_id,chat_id,raw_id,chat_kind) VALUES($1,-10,10,'group')",
        "INSERT INTO group_roster_crawls(telegram_account_id,chat_id,run_id) VALUES($1,-10,'run')",
        "INSERT INTO roster_crawl_seen VALUES($1,-10,'run',7,false)",
        "INSERT INTO membership_departures VALUES($1,-10,8,1,now(),'left')",
        "INSERT INTO channels(telegram_account_id,channel_telegram_id,channel_link) VALUES($1,-77,'https://example.invalid/channel')",
        "INSERT INTO group_watches(telegram_account_id,chat_id,channel_telegram_id,channel_link) VALUES($1,-10,-77,'https://example.invalid/channel')",
        "INSERT INTO daily_stats(telegram_account_id,stat_date) VALUES($1,current_date)",
        "INSERT INTO user_account_tombstones VALUES($1,8,'bot',now())",
        "INSERT INTO download_request_windows VALUES($1,7,'download',ARRAY[now()])",
    ]
    for statement in statements:
        await app.pool.execute(statement, app.bot)
    await app.pool.execute(
        "INSERT INTO site_accounts(site,label,credential_env) VALUES('test','local','TEST_CREDENTIAL')"
    )
    # Represents a prior token pointing at a different bot in the same installation.
    await app.pool.execute(
        "INSERT INTO bot_users(telegram_account_id,user_id,is_admin) VALUES($1,99,true)",
        app.bot + 1,
    )


async def test_reset_empties_every_model_across_bots_and_redis_but_preserves_schema(app, reset):
    await seed_all_tables(app)
    counts = await reset.repository.counts()
    assert len(counts) == 30 and all(counts.values())
    ledger = await app.pool.fetch("SELECT * FROM app_schema_migrations ORDER BY version")
    tables = await app.pool.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
    )
    assert {row["tablename"] for row in tables} == {*TABLE_NAMES, "app_schema_migrations"}
    foreign_key = f"unrelated:{app.bot}:keep"
    keys = [
        f"{prefix}:{bot}:old"
        for prefix in ("cache:v3", "state:v1", "media", "request")
        for bot in (app.bot, app.bot + 1)
    ]
    try:
        await app.redis.mset({key: "old" for key in [*keys, foreign_key]})
        token, preview = await reset.service.prepare(999)
        assert all(preview[name] >= count for name, count in counts.items())
        result = await reset.service.confirm(999, token)
        assert len(result) == 30 and sum(result.values()) > 30
        assert set((await reset.repository.counts()).values()) == {0}
        assert await app.redis.exists(*keys) == 0
        assert await app.redis.get(foreign_key) == b"old"
        assert reset.service.stopped and reset.gate.closed
        reset.deps["workers"][0].__aenter__.assert_not_awaited()
        assert (
            await app.pool.fetch("SELECT * FROM app_schema_migrations ORDER BY version") == ledger
        )
        await migrate(app.pool)
        assert set((await reset.repository.counts()).values()) == {0}
        # Table-owned sequences restart without losing definitions or constraints.
        assert (
            await app.pool.fetchval(
                "INSERT INTO site_accounts(site,label,credential_env) VALUES('new','new','NEW_CREDENTIAL') RETURNING id"
            )
            == 1
        )
        await app.pool.execute("DELETE FROM site_accounts")
    finally:
        await app.redis.delete(*keys, foreign_key)


@pytest.mark.parametrize("owner", [7, 8])
async def test_delegated_and_ordinary_users_cannot_prepare_or_confirm(app, reset, owner):
    await app.admin.mutate(999, "grant", 7)
    token, _ = await reset.service.prepare(999)
    for call in (
        lambda: reset.service.prepare(owner),
        lambda: reset.service.confirm(owner, token),
        lambda: reset.service.cancel(owner, token),
    ):
        with pytest.raises(DownloadError) as error:
            await call()
        assert error.value.code == "admin_denied"
    assert not reset.service.stopped
    assert await app.pool.fetchval("SELECT count(*) FROM bot_groups") == 1


@pytest.mark.parametrize(
    "invalid", ["cancelled", "expired", "wrong_action", "other_owner", "missing"]
)
async def test_invalid_confirmation_never_erases_or_stops(app, reset, invalid):
    token, _ = await reset.service.prepare(999)
    if invalid == "cancelled":
        await reset.service.cancel(999, token)
    elif invalid == "expired":
        await app.pool.execute("UPDATE admin_requests SET expires_at=now()-interval '1 second'")
    elif invalid == "wrong_action":
        await app.pool.execute("UPDATE admin_requests SET action='leave'")
    elif invalid == "other_owner":
        await app.pool.execute("UPDATE admin_requests SET owner_id=7")
    else:
        token = "missing"
    with pytest.raises(DownloadError):
        await reset.service.confirm(999, token)
    assert not reset.service.stopped and not reset.gate.closed
    assert await app.pool.fetchval("SELECT count(*) FROM bot_groups") == 1


async def test_active_transfer_prevents_reset_without_consuming_confirmation(app, reset):
    token, _ = await reset.service.prepare(999)
    reset.deps["jobs"].count = 1
    with pytest.raises(DownloadError, match="انتقال"):
        await reset.service.confirm(999, token)
    assert not reset.service.stopped
    assert await app.pool.fetchval(
        "SELECT consumed_at IS NULL FROM admin_requests WHERE token=$1", token
    )


@pytest.mark.parametrize("legacy", [False, True])
async def test_other_live_coordinator_prevents_global_reset(app, reset, legacy):
    token, _ = await reset.service.prepare(999)
    async with app.pool.acquire() as other:
        await other.execute(
            "SELECT pg_advisory_lock($1::bigint)", app.bot + 1
        ) if legacy else await DatabaseResetRepository(other, app.bot + 1).join_runtime()
        with pytest.raises(DownloadError):
            await reset.service.confirm(999, token)
    assert not reset.service.stopped
    assert await app.pool.fetchval("SELECT count(*) FROM bot_groups") == 1
    assert not reset.gate.paused


async def test_external_table_foreign_key_aborts_without_cascading_into_it(app, reset):
    await app.pool.execute(
        "CREATE TABLE external_data(id bigint PRIMARY KEY,bot bigint,chat bigint,FOREIGN KEY(bot,chat) REFERENCES bot_groups)"
    )
    await app.pool.execute("INSERT INTO external_data VALUES(1,$1,-10)", app.bot)
    token, _ = await reset.service.prepare(999)
    with pytest.raises(asyncpg.FeatureNotSupportedError):
        await reset.service.confirm(999, token)
    assert await app.pool.fetchval("SELECT count(*) FROM bot_groups") == 1
    assert await app.pool.fetchval("SELECT count(*) FROM external_data") == 1
    assert not await reset.repository.pending()
    assert reset.gate.closed


async def test_redis_failure_before_erase_preserves_all_sql_and_runtime(app, reset, monkeypatch):
    token, _ = await reset.service.prepare(999)
    monkeypatch.setattr(
        reset.service.redis, "verify", AsyncMock(side_effect=RedisConnectionError("offline"))
    )
    with pytest.raises(RedisConnectionError):
        await reset.service.confirm(999, token)
    assert await app.pool.fetchval("SELECT count(*) FROM bot_groups") == 1
    assert not reset.service.stopped


async def test_failure_after_truncate_rolls_back_data_token_and_identity(app, reset):
    await seed_all_tables(app)
    token, _ = await reset.service.prepare(999)
    original = reset.repository.connection

    class Failure:
        def __getattr__(self, name):
            return getattr(original, name)

        async def execute(self, query, *args):
            if "INSERT INTO admin_requests" in query:
                raise RuntimeError("injected after truncate")
            return await original.execute(query, *args)

    reset.repository.connection = Failure()
    with pytest.raises(RuntimeError, match="injected after truncate"):
        await reset.service.confirm(999, token)
    assert all((await reset.repository.counts()).values())
    assert await app.pool.fetchval(
        "SELECT consumed_at IS NULL FROM admin_requests WHERE token=$1", token
    )
    assert not await reset.repository.pending()
    assert (
        await app.pool.fetchval(
            "INSERT INTO site_accounts(site,label,credential_env) VALUES('next','next','NEXT_CREDENTIAL') RETURNING id"
        )
        == 2
    )
    assert reset.service.stopped


async def test_redis_failure_after_commit_leaves_recoverable_fence_for_new_bot(
    app, reset, monkeypatch
):
    token, _ = await reset.service.prepare(999)
    key = f"state:v1:{app.bot}:membership:0"
    await app.redis.xadd(key, {"event": "old"})
    original = reset.service.redis.purge
    monkeypatch.setattr(
        reset.service.redis, "purge", AsyncMock(side_effect=RedisConnectionError("offline"))
    )
    try:
        with pytest.raises(RedisConnectionError):
            await reset.service.confirm(999, token)
        assert reset.gate.closed and reset.service.stopped
        assert await app.pool.fetchval("SELECT count(*) FROM bot_groups") == 0
        assert len(await reset.repository.pending()) == 1
        with pytest.raises(RuntimeError, match="state Redis"):
            await recover_reset(reset.repository, ResetRedis(app.redis, None))
        assert len(await reset.repository.pending()) == 1
        monkeypatch.setattr(reset.service.redis, "purge", original)
        # Startup with a different bot still completes the old bot's reset first.
        await recover_reset(
            DatabaseResetRepository(reset.repository.connection, app.bot + 1), reset.service.redis
        )
        assert not await reset.repository.pending() and not await app.redis.exists(key)
        assert set((await reset.repository.counts()).values()) == {0}
    finally:
        await app.redis.delete(key)


async def test_new_runtime_cannot_enter_during_exclusive_reset(app, reset):
    async with reset.repository.exclusive(), app.pool.acquire() as other:
        assert not await other.fetchval(
            "SELECT pg_try_advisory_lock_shared($1::bigint)", RESET_LOCK
        )


async def test_reset_ui_has_explicit_scope_owner_checks_and_stops_after_success(app, reset):
    actual = event()
    await reset_callback(actual, app.admin, "resetprepare", "0")
    copy = actual.respond.call_args.args[0]
    assert "همهٔ ربات" in copy and "توکن جدید" in copy and "30" in copy
    rows = actual.respond.call_args.kwargs["buttons"]
    button = rows[-1][0]
    other = event(owner=7)
    other.data = button.type.data
    await ui.callback(other, app.admin)
    assert not reset.service.stopped and other.answer.call_args.kwargs["alert"]
    actual = event()
    actual.data = button.type.data
    await ui.callback(actual, app.admin)
    assert "پاکسازی کامل انجام شد" in actual.edit.call_args.args[0]
    reset.service.shutdown.assert_awaited_once()
    assert set((await reset.repository.counts()).values()) == {0}


async def test_failed_final_telegram_edit_still_stops_sealed_runtime(app, reset):
    token, _ = await reset.service.prepare(999)
    actual = event()
    actual.data = f"adm:999:resetconfirm:{token}".encode()
    actual.edit.side_effect = RuntimeError("Telegram unavailable")
    with pytest.raises(RuntimeError):
        await reset_callback(actual, app.admin, "resetconfirm", token)
    reset.service.shutdown.assert_awaited_once()
    assert reset.service.stopped


async def test_reset_confirmation_does_not_hold_the_owner_lock_while_draining_updates(app, reset):
    from bot.admin_keyboard import locks
    from services.conversation import ConversationService

    async def drain(owner, token):
        async with asyncio.timeout(1), locks.hold(str(owner)):
            return {}

    reset.service.confirm = AsyncMock(side_effect=drain)
    actual = event()
    actual.data = b"adm:999:resetconfirm:token"
    await ui.callback(actual, app.admin, conversations=ConversationService(app.workflow))
    reset.service.confirm.assert_awaited_once_with(999, "token")
    assert "پاکسازی کامل انجام شد" in actual.edit.call_args.args[0]


async def test_concurrent_confirmations_execute_the_reset_exactly_once(app, reset):
    token, _ = await reset.service.prepare(999)
    results = await asyncio.gather(
        *(reset.service.confirm(999, token) for _ in range(100)), return_exceptions=True
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, DownloadError) for result in results) == 99
    assert set((await reset.repository.counts()).values()) == {0}


async def test_reply_category_opens_only_a_native_confirmation_without_deleting(app, reset):
    from telethon import events, types

    from bot.handlers import admin_reference as panel
    from bot.texts import admin_buttons as labels
    from core.enums import ConversationState as CS
    from services.conversation import ConversationService

    conversations = ConversationService(app.workflow)
    await conversations.set_state(999, 999, CS.ADMIN_CAT_CLEANUP_PANEL)
    actual = event(labels.RESET_DATABASE)
    with pytest.raises(events.StopPropagation):
        await panel.handle_message(actual, app.admin, conversations)
    for row in actual.respond.call_args.kwargs["buttons"]:
        for item in row:
            assert isinstance(item.type, types.InlineButtonTypeCallback)
            assert len(item.type.data) <= 64
    assert not reset.service.stopped
    assert await app.pool.fetchval("SELECT count(*) FROM bot_groups") == 1


async def test_no_redis_configuration_can_still_complete_sql_only_reset(app, reset):
    reset.service.redis = ResetRedis()
    token, _ = await reset.service.prepare(999)
    await reset.service.confirm(999, token)
    assert set((await reset.repository.counts()).values()) == {0}


async def test_persistence_failure_keeps_recovery_marker_even_after_keys_are_deleted(
    app, reset, monkeypatch
):
    import repositories.redis.database_reset as redis_reset

    token, _ = await reset.service.prepare(999)
    key = f"state:v1:{app.bot}:membership:0"
    await app.redis.xadd(key, {"event": "old"})
    original = redis_reset.persist_deletions
    monkeypatch.setattr(
        redis_reset, "persist_deletions", AsyncMock(side_effect=TimeoutError("fsync not confirmed"))
    )
    try:
        with pytest.raises(TimeoutError):
            await reset.service.confirm(999, token)
        assert not await app.redis.exists(key)
        assert len(await reset.repository.pending()) == 1 and reset.service.stopped
        monkeypatch.setattr(redis_reset, "persist_deletions", original)
        await recover_reset(reset.repository, reset.service.redis)
        assert set((await reset.repository.counts()).values()) == {0}
    finally:
        await app.redis.delete(key)


async def test_persistence_barrier_covers_retry_on_one_pinned_connection():
    from repositories.redis.database_reset import persist_deletions

    client = SimpleNamespace(
        info=AsyncMock(return_value={"aof_enabled": 1}),
        execute_command=AsyncMock(side_effect=[[0, 0], [1, 0]]),
        save=AsyncMock(),
    )
    await persist_deletions(client)
    assert client.execute_command.await_count == 2
    client.save.assert_not_awaited()
    client.execute_command.side_effect = [[0, 0]] * 3
    with pytest.raises(TimeoutError):
        await persist_deletions(client)


async def test_unavailable_redis_reports_failure_without_erasing_or_stopping(app, reset):
    token, _ = await reset.service.prepare(999)
    reset.service.redis.verify = AsyncMock(side_effect=RedisConnectionError("offline"))
    actual = event()
    actual.data = f"adm:999:resetconfirm:{token}".encode()
    with pytest.raises(RedisConnectionError):
        await reset_callback(actual, app.admin, "resetconfirm", token)
    assert "داده‌ها حذف نشدند" in actual.edit.call_args.args[0]
    assert not reset.service.stopped
    reset.service.shutdown.assert_not_awaited()


async def test_sealed_gate_rejects_waiting_and_new_updates_without_restarting_workers():
    gate, deps = MaintenanceGate(), resources()
    gate.ready = True
    entered, release = asyncio.Event(), asyncio.Event()

    async def erase():
        async with gate.operation(), gate.guard(**deps):
            entered.set()
            await release.wait()
            gate.seal()

    async def update():
        async with gate.operation():
            pytest.fail("writer resumed after destructive reset")

    task = asyncio.create_task(erase())
    await entered.wait()
    writer = asyncio.create_task(update())
    await asyncio.sleep(0)
    release.set()
    await task
    with pytest.raises(DownloadError):
        await writer
    with pytest.raises(DownloadError):
        await update()
    deps["workers"][0].__aenter__.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, "disconnect", "cleanup"])
async def test_intentional_shutdown_does_not_recover_into_a_new_writer(monkeypatch, failure):
    trace = []

    class Container:
        stop_requested = True

        async def __aenter__(self):
            trace.append("enter")
            self.client = SimpleNamespace(
                run_until_disconnected=AsyncMock(
                    side_effect=OSError("disconnected") if failure == "disconnect" else None
                )
            )
            return self

        async def __aexit__(self, *args):
            trace.append("close")
            if failure == "cleanup":
                raise OSError("cleanup disconnected")

    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    await run_with_recovery(Container)
    assert trace == ["enter", "close"]
    sleep.assert_not_awaited()
