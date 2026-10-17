"""Complete archive scope, private-file lifecycle and a real isolated restore."""

import asyncio
import os
import shutil
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import asyncpg
import pytest

from db.postgres.engine import migrate
from db.postgres.models import TABLES
from repositories.postgres.workflow import WorkflowRepository
from schemas.media import DownloadError
from services.admin_maintenance import AdminMaintenance
from services.database_backup import DatabaseBackup


def test_dump_command_has_no_table_account_or_row_filter_and_no_password_in_argv(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/tools/pg_dump")
    backup = DatabaseBackup(
        "postgresql://admin:p%40ssword@db.example:5434/bot%20data?sslmode=require"
    )
    command, environment = backup.command()
    assert "--format=custom" in command
    assert "--dbname=bot data" in command
    assert "--host=db.example" in command and "--port=5434" in command
    assert environment["PGPASSWORD"] == "p@ssword"
    assert environment["PGSSLMODE"] == "require"
    assert not any("p@ssword" in arg or "p%40ssword" in arg for arg in command)
    assert not any(
        arg.startswith(("--table", "--schema", "--exclude", "--data-only")) for arg in command
    )


def test_compose_fallback_is_only_used_for_the_configured_local_database(monkeypatch):
    monkeypatch.setenv("POSTGRES_PORT", "55449")
    monkeypatch.setattr(shutil, "which", lambda name: "/tools/docker" if name == "docker" else None)
    command, _ = DatabaseBackup("postgresql://downloader@127.0.0.1:55449/downloader").command()
    start = command.index("exec")
    assert command[start : start + 3] == ["exec", "-T", "postgres"]
    assert "pg_dump" in command and "--format=custom" in command
    for dsn in (
        "postgresql://downloader@remote.example:55449/downloader",
        "postgresql://downloader@127.0.0.1:5432/other",
    ):
        with pytest.raises(DownloadError):
            DatabaseBackup(dsn).command()


async def test_successful_archive_lives_through_upload_and_is_deleted_even_when_upload_fails(
    monkeypatch,
):
    backup = DatabaseBackup("unused")
    monkeypatch.setattr(
        backup,
        "command",
        lambda: (
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'PGDMParchive')"],
            os.environ.copy(),
        ),
    )
    path = None
    with pytest.raises(RuntimeError, match="upload rejected"):
        async with backup.create() as path:
            assert await asyncio.to_thread(path.read_bytes) == b"PGDMParchive"
            if os.name != "nt":
                assert (await asyncio.to_thread(path.stat)).st_mode & 0o077 == 0
            raise RuntimeError("upload rejected")
    assert not path.exists()


@pytest.mark.parametrize(
    "script",
    [
        "import sys; sys.stdout.buffer.write(b'PGDMPpartial'); sys.exit(1)",
        "import sys; sys.stdout.buffer.write(b'invalid')",
        "import time; time.sleep(10)",
    ],
)
async def test_failure_timeout_and_bad_header_never_yield_partial_archives(
    monkeypatch, tmp_path, script
):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    backup = DatabaseBackup("unused", timeout=0.2)
    monkeypatch.setattr(
        backup, "command", lambda: ([sys.executable, "-c", script], os.environ.copy())
    )
    with pytest.raises(DownloadError):
        async with backup.create():
            pytest.fail("A failed dump must never become an upload")
    assert not list(tmp_path.iterdir())


async def test_cancellation_kills_the_child_and_removes_private_output(monkeypatch, tmp_path):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    backup = DatabaseBackup("unused")
    monkeypatch.setattr(
        backup,
        "command",
        lambda: ([sys.executable, "-c", "import time; time.sleep(60)"], os.environ.copy()),
    )
    process_started = asyncio.Event()
    spawn = asyncio.create_subprocess_exec
    processes = []

    async def start(*args, **options):
        process = await spawn(*args, **options)
        processes.append(process)
        process_started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)

    async def export():
        async with backup.create():
            pytest.fail("Cancelled dump became an upload")

    task = asyncio.create_task(export())
    await asyncio.wait_for(process_started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None
    assert not list(tmp_path.iterdir())


async def test_full_export_checks_root_before_creating_a_file():
    admin = SimpleNamespace(authorize=AsyncMock(side_effect=DownloadError("denied")))
    maintenance = AdminMaintenance(admin)
    maintenance.database_backup = SimpleNamespace(create=AsyncMock())
    with pytest.raises(DownloadError, match="denied"):
        async with maintenance.full_backup(7):
            pytest.fail("Non-root export")
    admin.authorize.assert_awaited_once_with(7, root=True)
    maintenance.database_backup.create.assert_not_called()


async def run_tool(command, *, environment):
    process = await asyncio.create_subprocess_exec(
        *command,
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await process.wait() == 0


async def test_complete_database_restores_every_model_other_bot_and_unregistered_schema():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn or not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        pytest.skip("Dedicated PostgreSQL and native pg_dump/pg_restore are required")
    prefix = "full_backup_" + uuid.uuid4().hex
    source_name, target_name = prefix + "_source", prefix + "_restore"
    administrator = await asyncpg.connect(dsn)
    created = []
    try:
        for database in (source_name, target_name):
            await administrator.execute(f'CREATE DATABASE "{database}"')
            created.append(database)
        url = urlsplit(dsn)
        source_dsn = url._replace(path="/" + source_name).geturl()
        target_dsn = url._replace(path="/" + target_name).geturl()
        pool = await asyncpg.create_pool(source_dsn, min_size=1, max_size=2)
        try:
            await migrate(pool)
            await WorkflowRepository(pool, 101).touch_user(7, started=True)
            await WorkflowRepository(pool, 202).touch_user(8, started=True)
            await pool.execute("CREATE SCHEMA extra_backup_probe")
            await pool.execute(
                "CREATE TABLE extra_backup_probe.future_table (id serial PRIMARY KEY, data bytea)"
            )
            await pool.execute(
                "INSERT INTO extra_backup_probe.future_table(data) VALUES($1)",
                b"dummy-file-reference",
            )
        finally:
            await pool.close()
        async with DatabaseBackup(source_dsn).create() as archive:
            destination, environment = DatabaseBackup(target_dsn).command()
            connection = [
                arg
                for arg in destination[1:]
                if arg.startswith(("--username=", "--host=", "--port=", "--dbname="))
            ]
            await run_tool(
                [
                    shutil.which("pg_restore"),
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                    *connection,
                    str(archive),
                ],
                environment=environment,
            )
        assert not archive.exists()
        restored = await asyncpg.connect(target_dsn)
        try:
            tables = await restored.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
            assert {table.name for table in TABLES} <= {row["tablename"] for row in tables}
            users = await restored.fetch(
                "SELECT telegram_account_id,user_id FROM bot_users ORDER BY telegram_account_id"
            )
            assert [tuple(row) for row in users] == [(101, 7), (202, 8)]
            assert (
                await restored.fetchval("SELECT data FROM extra_backup_probe.future_table")
                == b"dummy-file-reference"
            )
            assert (
                await restored.fetchval(
                    "INSERT INTO extra_backup_probe.future_table DEFAULT VALUES RETURNING id"
                )
                == 2
            )
        finally:
            await restored.close()
    finally:
        for database in reversed(created):
            await administrator.execute(f'DROP DATABASE "{database}"')
        await administrator.close()
