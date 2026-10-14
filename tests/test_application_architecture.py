"""Keep the Couplyo layer direction explicit during future edits."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_application_layers_have_one_canonical_implementation():
    for folder in (
        "core",
        "db/postgres",
        "db/postgres/models",
        "db/redis",
        "repositories/interfaces",
        "repositories/postgres",
        "repositories/redis",
        "schemas",
        "services",
        "bot/handlers",
        "bot/state",
        "bot/checks",
        "bot/keyboards",
        "bot/texts",
        "bot/cards",
    ):
        assert (ROOT / folder / "__init__.py").is_file()
    assert (ROOT / "container.py").is_file()
    assert (ROOT / "bot/jobs.py").is_file()
    assert (ROOT / "bot/handlers/dispatcher.py").is_file()
    assert (ROOT / "repositories/postgres/unit_of_work.py").is_file()
    package = ROOT / "src/downloader_bot"
    assert {p.name for p in package.glob("*.py")} == {"__init__.py", "__main__.py"}
    for folder in ("core", "db", "repositories", "schemas", "services", "bot"):
        assert not list((package / folder).rglob("*.py"))
    assert len((package / "__main__.py").read_text().splitlines()) < 65


def test_handlers_do_not_execute_queries_or_import_concrete_repositories():
    for path in (ROOT / "bot/handlers").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("db", "repositories"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"execute", "fetchrow", "fetchval", "transaction"}


def test_postgres_repositories_do_not_depend_on_telegram_or_redis():
    for path in (ROOT / "repositories/postgres").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("telethon", "redis", "bot"))


def test_business_services_do_not_import_the_telegram_framework():
    for path in (ROOT / "services").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("telethon", "bot"))


def test_core_does_not_import_provider_or_application_implementations():
    for path in (ROOT / "core").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    ("downloader_bot.downloaders", "services", "bot")
                )
