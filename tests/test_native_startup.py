"""Verify native configuration, stable database ports and literal credentials."""

import importlib.util
import json
import socket
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load_module("setup_services", ROOT / "tools/setup_services.py")
entry = load_module("native_entry", ROOT / "main.py")


def create_env(tmp_path, **values):
    path = tmp_path / ".env"
    path.write_text("# Local settings\n", encoding="utf-8")
    for key, value in values.items():
        set_key(path, key, value, quote_mode="always")
    return path


def test_missing_ports_are_chosen_once_and_saved_as_explicit_localhost_urls(tmp_path):
    path = tmp_path / ".env"
    first = setup.configure_environment(path, ROOT / ".env.example")
    second = setup.configure_environment(path, ROOT / ".env.example")
    saved = dotenv_values(path, interpolate=False)
    assert first["POSTGRES_PORT"] != first["REDIS_PORT"]
    for key in ("POSTGRES_PORT", "REDIS_PORT", "DEV_DB_PASSWORD", "DATABASE_URL", "REDIS_URL"):
        assert first[key] == second[key] == saved[key]
    assert len(saved["DEV_DB_PASSWORD"]) == 64
    for port_key, url_key in (("POSTGRES_PORT", "DATABASE_URL"), ("REDIS_PORT", "REDIS_URL")):
        parsed = urlsplit(saved[url_key])
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port == int(saved[port_key])


@pytest.mark.parametrize(
    "password", ["literal@host/#?:%$[x]\\word' space", "pass${HOME}$$", "plain"]
)
def test_password_is_preserved_and_encoded_without_corrupting_host_or_port(tmp_path, password):
    path = create_env(tmp_path, DEV_DB_PASSWORD=password, POSTGRES_PORT="32768", REDIS_PORT="32769")
    values = setup.configure_environment(path, ROOT / ".env.example")
    saved = dotenv_values(path, interpolate=False)
    parsed = urlsplit(values["DATABASE_URL"])
    assert parsed.hostname == "127.0.0.1" and parsed.port == 32768
    assert unquote(parsed.password) == password == saved["DEV_DB_PASSWORD"]
    assert saved["POSTGRES_PORT"] == "32768" and saved["REDIS_PORT"] == "32769"


def test_migration_retains_existing_localhost_ports_and_replaces_docker_hostname(tmp_path):
    path = create_env(
        tmp_path,
        DEV_DB_PASSWORD="unchanged",
        DATABASE_URL="postgresql://u:p@127.0.0.1:32123/downloader",
        REDIS_URL="redis://127.0.0.1:32124/0",
    )
    saved = setup.configure_environment(path, ROOT / ".env.example")
    assert saved["POSTGRES_PORT"] == "32123" and saved["REDIS_PORT"] == "32124"
    set_key(path, "DATABASE_URL", "postgresql://u:p@postgres:5432/downloader")
    again = setup.configure_environment(path, ROOT / ".env.example")
    assert urlsplit(again["DATABASE_URL"]).hostname == "127.0.0.1"
    assert urlsplit(again["DATABASE_URL"]).port == 32123
    assert again["DEV_DB_PASSWORD"] == "unchanged"


def test_random_selection_avoids_an_already_occupied_port(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        number = occupied.getsockname()[1]
        values = setup.configure_environment(tmp_path / ".env", ROOT / ".env.example")
        assert number not in {int(values["POSTGRES_PORT"]), int(values["REDIS_PORT"])}


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_invalid_explicit_port_fails_without_replacing_password(tmp_path, port):
    path = create_env(tmp_path, DEV_DB_PASSWORD="keep-this", POSTGRES_PORT=port)
    with pytest.raises(RuntimeError, match="POSTGRES_PORT"):
        setup.configure_environment(path, ROOT / ".env.example")
    assert dotenv_values(path)["DEV_DB_PASSWORD"] == "keep-this"


def test_duplicate_ports_are_rejected(tmp_path):
    path = create_env(tmp_path, POSTGRES_PORT="32768", REDIS_PORT="32768")
    with pytest.raises(RuntimeError, match="different"):
        setup.configure_environment(path, ROOT / ".env.example")


def test_missing_postgres_port_cannot_take_the_explicit_redis_port(tmp_path, monkeypatch):
    ports = iter((32769, 32770))

    class Reservation:
        def __enter__(self):
            self.port = next(ports)
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            pass

        def getsockname(self):
            return "127.0.0.1", self.port

    monkeypatch.setattr(setup.socket, "socket", Reservation)
    path = create_env(tmp_path, REDIS_PORT="32769")
    values = setup.configure_environment(path, ROOT / ".env.example")
    assert values["REDIS_PORT"] == "32769" and values["POSTGRES_PORT"] == "32770"


def test_configure_only_prints_addresses_without_secrets(tmp_path, monkeypatch, capsys):
    password, token = "PASSWORD$#@SECRET", "BOT-TOKEN-SECRET"
    path = create_env(tmp_path, DEV_DB_PASSWORD=password, BOT_TOKEN=token)
    monkeypatch.setattr(
        sys, "argv", ["setup_services.py", "--env-file", str(path), "--configure-only"]
    )
    monkeypatch.setattr(
        setup, "start_services", lambda *args, **kwargs: pytest.fail("Docker invoked")
    )
    assert setup.main() == 0
    output = capsys.readouterr().out
    assert "127.0.0.1:" in output and password not in output and token not in output


def test_native_entry_reads_its_own_env_literally_even_from_another_directory(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    token = "literal${HOME}token"
    create_env(root, API_ID="123", API_HASH="test-hash", BOT_TOKEN=token, DATABASE_URL="local-db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry, "__file__", str(root / "main.py"))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    for key in ("API_ID", "API_HASH", "BOT_TOKEN", "DATABASE_URL"):
        monkeypatch.setenv(key, "stale-exported-value")
    calls = []

    def application(name, *, run_name):
        from downloader_bot.config import Settings

        settings = Settings.from_environment()
        assert settings.api_id == 123 and settings.bot_token == token
        assert settings.database_url == "local-db"
        assert Path.cwd() == root
        calls.append((name, run_name))

    monkeypatch.setattr(entry.runpy, "run_module", application)
    entry.main()
    assert calls == [("downloader_bot", "__main__")]


async def test_startup_wait_logs_explain_cooldown_without_credentials(monkeypatch):
    from types import SimpleNamespace

    from telethon import errors

    from downloader_bot.__main__ import sign_in_bot

    records, calls = [], []

    async def sign_in(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise errors.FloodWaitError(request=None, capture=12)

    async def sleep(seconds):
        assert seconds == 12

    monkeypatch.setattr("asyncio.sleep", sleep)
    telemetry = SimpleNamespace(
        emit=lambda event, **fields: records.append({"event": event, **fields})
    )
    await sign_in_bot(SimpleNamespace(sign_in=sign_in), "SECRET-TOKEN", telemetry)
    assert records == [{"event": "telegram_login_wait", "wait_seconds": 12}]
    assert "SECRET-TOKEN" not in json.dumps(records)
